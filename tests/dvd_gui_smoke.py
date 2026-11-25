#!/usr/bin/env python3
"""Deterministic offscreen smoke test for dvd_player GUI sections 5-6."""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QCloseEvent, QColor, QImage, QKeyEvent, QMouseEvent
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


# ── QMessageBox recorders (never block) ─────────────────────────────
_dialogs: dict[str, list[tuple[object, ...]]] = {
    "warning": [],
    "critical": [],
    "question": [],
}
_question_answer = QMessageBox.StandardButton.Yes


def _record_warning(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
    _dialogs["warning"].append(args)
    return QMessageBox.StandardButton.Ok


def _record_critical(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
    _dialogs["critical"].append(args)
    return QMessageBox.StandardButton.Ok


def _record_question(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
    _dialogs["question"].append(args)
    return _question_answer


QMessageBox.warning = staticmethod(_record_warning)  # type: ignore[method-assign]
QMessageBox.critical = staticmethod(_record_critical)  # type: ignore[method-assign]
QMessageBox.question = staticmethod(_record_question)  # type: ignore[method-assign]

# ── (a) Xbox360Osd painting, hit-testing and formatting ─────────────
osd = dp.Xbox360Osd()
osd.resize(1280, 64)
osd.set_transport(
    state=dp.PlayerState.PLAYING,
    speed=4.0,
    time_pos=65.0,
    duration=5400.0,
    chapter=3,
    chapter_count=12,
    audio_lang="en",
    subtitle_lang=None,
)
pixmap = osd.grab()
check(not pixmap.isNull(), "osd.grab produced a null pixmap")
check(pixmap.width() == 1280 and pixmap.height() == 64, "grab size mismatch")

image = QImage(1280, 64, QImage.Format.Format_ARGB32)
osd.render(image)
check(not image.isNull(), "osd.render into QImage failed")

rects = osd._button_rects()
check(len(rects) == 10, f"expected 10 button rects, got {len(rects)}")
check(
    [name for name, _ in rects] == [name for name, _ in dp.Xbox360Osd.BUTTONS],
    "button rect order does not match BUTTONS",
)

fired: list[str] = []
osd.button_clicked.connect(fired.append)


def press(name: str) -> None:
    rect = dict(osd._button_rects())[name]
    centre = rect.center()
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(centre),
        QPointF(centre),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    osd.mousePressEvent(event)


for required in ("prev", "play_pause", "eject"):
    before = len(fired)
    press(required)
    check(len(fired) == before + 1, f"{required} did not emit")
    check(fired[-1] == required, f"wrong signal name for {required}: {fired[-1]}")

# Eject stands apart: an extra BUTTON_GAP separates it from 'menu'.
_rect_map = dict(osd._button_rects())
_eject_gap = _rect_map["eject"].left() - _rect_map["menu"].right()
_normal_gap = _rect_map["menu"].left() - _rect_map["subtitle"].right()
check(
    _eject_gap >= 2.0 * _normal_gap,
    f"eject gap {_eject_gap} not >= 2x normal gap {_normal_gap}",
)

before = len(fired)
outside = QMouseEvent(
    QEvent.Type.MouseButtonPress,
    QPointF(0.0, 0.0),
    QPointF(0.0, 0.0),
    Qt.MouseButton.LeftButton,
    Qt.MouseButton.LeftButton,
    Qt.KeyboardModifier.NoModifier,
)
osd.mousePressEvent(outside)
check(len(fired) == before, "click outside buttons emitted a signal")

move = QMouseEvent(
    QEvent.Type.MouseMove,
    QPointF(dict(osd._button_rects())["menu"].center()),
    QPointF(dict(osd._button_rects())["menu"].center()),
    Qt.MouseButton.NoButton,
    Qt.MouseButton.NoButton,
    Qt.KeyboardModifier.NoModifier,
)
osd.mouseMoveEvent(move)
check(osd._hover == "menu", "hover state not tracked")

check(osd._fmt_time(65.0) == "01:05", "mm:ss formatting broken")
check(osd._fmt_time(5400.0) == "1:30:00", "h:mm:ss formatting broken")
check(osd._fmt_time(0.0) == "00:00", "zero formatting broken")
check(osd._track_text() == "AUD en", f"track text: {osd._track_text()}")
osd.set_transport(
    state=dp.PlayerState.PLAYING,
    speed=4.0,
    time_pos=65.0,
    duration=5400.0,
    chapter=3,
    chapter_count=12,
    audio_lang="en",
    subtitle_lang="fr",
)
check(osd._track_text() == "AUD en · SUB fr", "track text with subtitle")
osd.grab()

# ── (b) OSD auto-hide and timer restart ─────────────────────────────
osd.show_osd()
check(osd.isVisible(), "show_osd did not show the widget")
deadline = time.monotonic() + (dp.OSD_TIMEOUT_MS + 300) / 1000.0
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
check(not osd.isVisible(), "OSD did not auto-hide after the timeout")

osd.show_osd()
osd.show_osd()
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
check(osd.isVisible(), "timer restart failed: OSD hid after 2 s")
osd.hide()

# ── (c) MainWindow construction and OSD geometry ────────────────────
engine = dp.DvdPlaybackEngine()
engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
window = dp.MainWindow(engine)
window._auto_detected = True
check(hasattr(window, "video"), "window.video missing")
check(hasattr(window, "osd"), "window.osd missing")
check(hasattr(window, "source"), "window.source missing")
check(hasattr(window, "status"), "window.status missing")
check(window.status.text() == "IDLE", "initial status is not IDLE")
check(
    window.minimumWidth() == 854 and window.minimumHeight() == 480,
    "window minimum size is not 854x480",
)
window.resize(1280, 720)
window.show()
app.processEvents()
app.processEvents()
check(not window.source.isVisible(), "bottom bar is visible by default")
check(not window.status.isVisible(), "bottom bar status label is visible by default")
x, y, width, height = window.osd.geometry().getRect()
check((x, y, width, height) == (0, 720 - 64, 1280, 64), f"osd geometry {x, y, width, height}")

# ── (d) key map dispatch ────────────────────────────────────────────
calls: list[tuple[str, tuple[object, ...]]] = []


def recorder(name: str):
    def _record(*args: object, **kwargs: object) -> None:
        calls.append((name, args))

    return _record


for method in (
    "menu_nav",
    "menu_select",
    "menu_mouse_move",
    "play_pause",
    "menu_back",
    "show_root_menu",
    "show_title_menu",
    "stop",
    "next_chapter",
    "previous_chapter",
    "cycle_audio",
    "cycle_subtitle",
    "adjust_volume",
    "eject",
    "set_speed",
    "cycle_aspect",
):
    setattr(engine, method, recorder(method))

key_cases: list[tuple[str, str, tuple[object, ...]]] = [
    ("Key_Up", "menu_nav", ("up",)),
    ("Key_Down", "menu_nav", ("down",)),
    ("Key_Left", "menu_nav", ("left",)),
    ("Key_Right", "menu_nav", ("right",)),
    ("Key_Space", "play_pause", ()),
    ("Key_Backspace", "menu_back", ()),
    ("Key_X", "show_root_menu", ()),
    ("Key_Y", "show_title_menu", ()),
    ("Key_S", "stop", ()),
    ("Key_N", "next_chapter", ()),
    ("Key_P", "previous_chapter", ()),
    ("Key_I", "cycle_audio", ()),
    ("Key_O", "cycle_subtitle", ()),
    ("Key_Plus", "adjust_volume", (5.0,)),
    ("Key_Equal", "adjust_volume", (5.0,)),
    ("Key_Minus", "adjust_volume", (-5.0,)),
    ("Key_E", "eject", ()),
    ("Key_A", "cycle_aspect", ()),
]
for key_name, method, expected_args in key_cases:
    calls.clear()
    consumed = window._handle_key(getattr(Qt.Key, key_name).value)
    check(consumed is True, f"{key_name} was not consumed")
    check(calls == [(method, expected_args)], f"{key_name} -> {calls}")
    check(
        window.osd.isVisible(),
        f"{key_name} outside the menu did not reveal the OSD",
    )

# Inside the menu keys dispatch but never reveal the OSD.
engine._menu_active = True
window.osd.hide()
calls.clear()
check(window._handle_key(Qt.Key.Key_Right.value) is True, "Right not consumed")
check(calls == [("menu_nav", ("right",))], f"Right in menu -> {calls}")
check(not window.osd.isVisible(), "menu key revealed the OSD")
calls.clear()
check(window._handle_key(Qt.Key.Key_Return.value) is True, "Return not consumed")
check(calls == [("menu_select", ())], f"Return in menu -> {calls}")
check(not window.osd.isVisible(), "Return in menu revealed the OSD")

# Outside the menu keys reveal the OSD again.
engine._menu_active = False
window.osd.hide()
calls.clear()
check(window._handle_key(Qt.Key.Key_Return.value) is True, "Return not consumed")
check(calls == [("play_pause", ())], f"Return outside menu -> {calls}")
check(window.osd.isVisible(), "Return outside menu did not reveal the OSD")

# Guide key M toggles the OSD anywhere, without dispatching an action.
calls.clear()
check(window._handle_key(Qt.Key.Key_M.value) is True, "M not consumed")
check(not window.osd.isVisible(), "M did not hide the visible OSD")
check(window._handle_key(Qt.Key.Key_M.value) is True, "M not consumed")
check(window.osd.isVisible(), "M did not reveal the OSD")
check(calls == [], f"M dispatched an action: {calls}")

# Entering the menu always hides the OSD (the menu is the interface).
window._on_menu_active(True)
check(not window.osd.isVisible(), "menu entry did not hide the OSD")
window._on_menu_active(False)
check(not window.osd.isVisible(), "menu exit revealed the OSD")

# Pointer activity reveals the OSD only outside the menu.
window.osd.hide()
engine._menu_active = False
window._on_video_mouse(
    window.video,
    QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    ),
)
check(window.osd.isVisible(), "mouse move outside menu did not reveal the OSD")
window.osd.hide()
engine._menu_active = True
calls.clear()
window._on_video_mouse(
    window.video,
    QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    ),
)
check(not window.osd.isVisible(), "mouse move in menu revealed the OSD")
check(calls == [("menu_mouse_move", (5, 5))], f"menu mouse move -> {calls}")
engine._menu_active = False
window.osd.hide()

calls.clear()
check(window._handle_key(Qt.Key.Key_Z.value) is False, "Key_Z should be unmapped")
check(calls == [], "Key_Z dispatched an action")

check(window._handle_key(Qt.Key.Key_Escape.value) is True, "Escape not consumed")
check(calls == [], "Escape dispatched an engine action")

# ── (e) OSD button routing ──────────────────────────────────────────
calls.clear()
window._on_osd_button("ff")
window._on_osd_button("rw")
check(calls == [("set_speed", (2.0,)), ("set_speed", (16.0,))], f"ff/rw -> {calls}")
check(engine.speed in dp.FF_SPEEDS or engine.speed == 1.0, "invalid speed")

calls.clear()
window._on_osd_button("menu")
check(calls == [("show_root_menu", ())], f"menu -> {calls}")

calls.clear()
_question_answer = QMessageBox.StandardButton.No
window.status.setText("")
window._on_osd_button("eject")
check(calls == [], f"eject No still ejected -> {calls}")
check(window.status.text() == "", "eject No changed status")
check(_dialogs["question"], "eject did not ask for confirmation")
_question_answer = QMessageBox.StandardButton.Yes
calls.clear()
window._on_osd_button("eject")
check(calls == [("eject", ())], f"eject -> {calls}")
check(window.status.text() == "EJECTED", "eject did not update status")

# Ctrl+L: browse dialog path loads immediately and fills the source field.
_orig_browse = window._browse_for_source
window._browse_for_source = lambda: "/tmp/picked.iso"  # type: ignore[method-assign]
loads_l: list[tuple[str, bool]] = []
window._load_source = lambda src, quiet=False: loads_l.append((src, quiet))  # type: ignore[method-assign]
from PyQt6.QtGui import QKeyEvent  # noqa: E402

_ctrl_l = QKeyEvent(
    QEvent.Type.KeyPress,
    Qt.Key.Key_L.value,
    Qt.KeyboardModifier.ControlModifier,
)
window.keyPressEvent(_ctrl_l)
check(window.source.text() == "/tmp/picked.iso", "Ctrl+L did not set the source text")
check(loads_l == [("/tmp/picked.iso", False)], f"Ctrl+L load -> {loads_l}")
window._browse_for_source = _orig_browse  # type: ignore[method-assign]

# Ctrl+L with no picked path loads nothing.
window._browse_for_source = lambda: None  # type: ignore[method-assign]
loads_l.clear()
window.keyPressEvent(_ctrl_l)
check(loads_l == [], "Ctrl+L loaded without a picked path")
window._browse_for_source = _orig_browse  # type: ignore[method-assign]

# ── (f) _sync_osd safety with an unattached engine ──────────────────
engine2 = dp.DvdPlaybackEngine()
engine2.attach_widget = lambda wid: None  # type: ignore[method-assign]
window2 = dp.MainWindow(engine2)
window2._auto_detected = True
check(engine2.player is None, "expected unattached engine")
window2._sync_osd()
check(True, "_sync_osd raised with unattached engine")

# ── (g) closeEvent disposes the engine ──────────────────────────────
engine3 = dp.DvdPlaybackEngine()
engine3.attach_widget = lambda wid: None  # type: ignore[method-assign]
window3 = dp.MainWindow(engine3)
window3._auto_detected = True
window3.show()
app.processEvents()
check(engine3._disposed is False, "engine disposed too early")
window3.close()
app.processEvents()
check(engine3._disposed is True, "closeEvent did not dispose the engine")

# ── (h) QMessageBox paths ───────────────────────────────────────────
engine4 = dp.DvdPlaybackEngine()
engine4.attach_widget = lambda wid: None  # type: ignore[method-assign]
window4 = dp.MainWindow(engine4)
window4._auto_detected = True
window4._attach_engine = lambda: None  # type: ignore[method-assign]
window4.source.setText("")
original_devices = dp.list_dvd_devices
dp.list_dvd_devices = lambda: []  # type: ignore[assignment]
_dialogs["warning"].clear()
window4._on_load_clicked()
check(len(_dialogs["warning"]) == 1, "empty source did not warn")
dp.list_dvd_devices = original_devices  # type: ignore[assignment]

load_calls: list[str] = []
resume_calls: list[dict[object, object]] = []
engine4.load = lambda src: load_calls.append(src)  # type: ignore[method-assign]
engine4.resume_disc = lambda saved: resume_calls.append(saved)  # type: ignore[method-assign]
window4._pending_source = "/tmp/fake-disc.iso"
_dialogs["question"].clear()
_question_answer = QMessageBox.StandardButton.Yes
window4._show_resume_prompt("deadbeef", {"pos": 65.0, "title": 1, "chapter": 2})
check(len(_dialogs["question"]) == 1, "resume prompt did not ask")
check(load_calls == [], f"resume prompt reloaded the disc -> {load_calls}")
check(resume_calls == [{"pos": 65.0, "title": 1, "chapter": 2}], "resume_disc not called")

_dialogs["question"].clear()
resume_calls.clear()
_question_answer = QMessageBox.StandardButton.No
window4._show_resume_prompt("deadbeef2", {"pos": 30.0})
check(load_calls == [], f"No branch reloaded the disc -> {load_calls}")
check(resume_calls == [], "No branch resumed")

# ── (i) disc auto-play / insertion polling ──────────────────────────
engine5 = dp.DvdPlaybackEngine()
engine5.attach_widget = lambda wid: None  # type: ignore[method-assign]
window5 = dp.MainWindow(engine5)
window5._auto_detected = True
auto_calls: list[tuple[str, bool]] = []
window5._load_source = lambda src, quiet=False: auto_calls.append((src, quiet))  # type: ignore[method-assign]
original_devices = dp.list_dvd_devices
original_present = dp.disc_present
dp.list_dvd_devices = lambda: ["/dev/sr0", "/dev/sr1"]  # type: ignore[assignment]
dp.disc_present = lambda dev: dev == "/dev/sr1"  # type: ignore[assignment]
window5._auto_detect_disc()
# A1: launch opens the dashboard and NEVER auto-loads.
check(auto_calls == [], f"auto detect loaded -> {auto_calls}")
check(not window5.home.isHidden(), "launch did not open HomeMenu")
check(window5.source.text() == "", "auto detect touched the source")
check(window5._disc_presence.get("/dev/sr1") is True,
      "auto detect did not prime the presence snapshot")

auto_calls.clear()
window5.home.hide()
dp.disc_present = lambda dev: True  # type: ignore[assignment]
window5._disc_presence = {"/dev/sr0": False, "/dev/sr1": True}  # post-auto-detect snapshot
window5._poll_disc_insertion()
# A1: insertion summons HomeMenu with a refreshed DVD blade; zero loads.
check(auto_calls == [], f"insertion edge loaded -> {auto_calls}")
check(not window5.home.isHidden(), "insertion edge did not open HomeMenu")
check(window5.home.blade_name() == "DVD", "HomeMenu did not open on the DVD blade")
check("/dev/sr0" in [payload for _text, payload in window5.home.entries()],
      "insertion did not refresh the DVD blade")

window5._loaded_source = "/dev/sr0"
dp.disc_present = lambda dev: False  # type: ignore[assignment]
window5._disc_presence = {"/dev/sr0": True}
window5._poll_disc_insertion()
window5._poll_disc_insertion()
check(window5.status.text() == "disc removed", "removal edge not reported")
dp.list_dvd_devices = original_devices  # type: ignore[assignment]
dp.disc_present = original_present  # type: ignore[assignment]

# ── (j) _load_source quiet/loud refactor ───────────────────────────
window5._auto_detected = True
engine6 = dp.DvdPlaybackEngine()
engine6.attach_widget = lambda wid: None  # type: ignore[method-assign]
window6 = dp.MainWindow(engine6)
window6._auto_detected = True
window6._attach_engine = lambda: None  # type: ignore[method-assign]
load6: list[str] = []
engine6.load = lambda src: load6.append(src)  # type: ignore[method-assign]

_orig_env = dp.check_environment
_orig_get = dp.ResumeStore.get
_orig_devices = dp.list_dvd_devices
_orig_present = dp.disc_present
dp.check_environment = lambda source: ["problem x"]  # type: ignore[assignment]

_dialogs["warning"].clear()
_dialogs["critical"].clear()
load6.clear()
window6._load_source("/tmp/fake", quiet=False)
# New spec: a nonexistent source is a synchronous critical error and the
# GUI never computes a hash or starts playback.
check(len(_dialogs["warning"]) == 0, "nonexistent source opened a warning")
check(len(_dialogs["critical"]) == 1, "nonexistent source did not open a critical")
check("cannot access disc source" in str(_dialogs["critical"][0]),
      "garbage path critical text missing")
check(load6 == [], f"garbage path loaded -> {load6}")
import tempfile as _tmpmod
with _tmpmod.TemporaryDirectory() as _td:
    _real = _tmpmod.NamedTemporaryFile(dir=_td, delete=False)
    _real.write(b"x")
    _real.close()
    _dialogs["warning"].clear()
    _dialogs["critical"].clear()
    load6.clear()
    window6._load_source(_real.name, quiet=False)
    check(len(_dialogs["warning"]) == 1, "existing source did not warn once")
    check(len(_dialogs["critical"]) == 0, "existing source opened a critical")
    check(load6 == [_real.name], f"loud load (existing) -> {load6}")

    _dialogs["warning"].clear()
    _dialogs["critical"].clear()
    _dialogs["question"].clear()
    load6.clear()
    window6._load_source(_real.name, quiet=True)
    check(len(_dialogs["warning"]) == 0, "quiet load opened a modal warning")
    check(len(_dialogs["question"]) == 0, "quiet load opened the resume modal")
    check(load6 == [_real.name], f"quiet load -> {load6}")
    check("problem x" in window6.status.toolTip(),
          "quiet load status missing problem")

_dialogs["critical"].clear()
load6.clear()
window6._load_source("/tmp/fake", quiet=True)
check(len(_dialogs["critical"]) == 0, "quiet garbage path opened a critical")
check(load6 == [], f"quiet garbage path loaded -> {load6}")
check("cannot access disc source" in window6.status.toolTip(),
      "quiet garbage path status missing")

# ── (j2) async disc hash -> gated resume prompt ────────────────────
_orig_hash_get = dp.ResumeStore.get
dp.ResumeStore.get = lambda self, h: {"pos": 65.0}  # type: ignore[method-assign]
_orig_prompt = window6._show_resume_prompt
prompts6: list[tuple[str, dict]] = []  # type: ignore[type-arg]
window6._show_resume_prompt = (  # type: ignore[method-assign]
    lambda h, saved: prompts6.append((h, saved))
)
engine6._sm._state = dp.PlayerState.LOADING
window6._on_disc_hash("early")
check(prompts6 == [("early", {"pos": 65.0})],
      f"early disc hash did not prompt: {prompts6}")

class _LatePlayer:
    time_pos = 120.0

    def terminate(self) -> None:
        pass

prompts6.clear()
engine6._player = _LatePlayer()  # type: ignore[assignment]
engine6._sm._state = dp.PlayerState.PLAYING
window6._load_started_at = time.monotonic() - 20.0  # late: 20 s since load
window6._on_disc_hash("late")
check(prompts6 == [], f"late disc hash prompted: {prompts6}")
dp.ResumeStore.get = _orig_hash_get  # type: ignore[method-assign]
window6._show_resume_prompt = _orig_prompt  # type: ignore[method-assign]

# ── (k) auto detect single device opens the dashboard ──────────────
auto6: list[tuple[str, bool]] = []
window6._load_source = lambda src, quiet=False: auto6.append((src, quiet))  # type: ignore[method-assign]
dp.list_dvd_devices = lambda: ["/dev/sr0"]  # type: ignore[assignment]
dp.disc_present = lambda dev: True  # type: ignore[assignment]
window6.source.setText("")
window6.home.hide()
window6._auto_detect_disc()
check(auto6 == [], f"auto detect single loaded -> {auto6}")
check(not window6.home.isHidden(), "auto detect single did not open HomeMenu")
check(window6.source.text() == "", "auto detect set the source")

auto6.clear()
window6.source.setText("")
dp.disc_present = lambda dev: False  # type: ignore[assignment]
window6.home.hide()
window6._auto_detect_disc()
check(auto6 == [], "auto detect loaded without a disc")
check(not window6.home.isHidden(), "launch without a disc did not open HomeMenu")
check(window6.source.text() == "", "auto detect touched source without a disc")

# ── (l) disc insertion edge summons the dashboard, never loads ─────
auto6.clear()
dp.disc_present = lambda dev: True  # type: ignore[assignment]
engine6._sm._state = dp.PlayerState.IDLE
window6.home.hide()
window6._disc_presence = {"/dev/sr0": False}
window6._poll_disc_insertion()
check(auto6 == [], f"insertion edge loaded -> {auto6}")
check(not window6.home.isHidden(), "insertion edge did not open HomeMenu")
window6._poll_disc_insertion()
check(auto6 == [], "insertion edge loaded on the second poll")
check(not window6.home.isHidden(), "HomeMenu not visible after repeat poll")

auto6.clear()
engine6._sm._state = dp.PlayerState.PLAYING
window6.home.hide()
window6._disc_presence = {"/dev/sr0": False}
window6._poll_disc_insertion()
check(auto6 == [], "insertion loaded while PLAYING")
check(window6.home.isHidden(), "insertion opened HomeMenu while PLAYING")
engine6._sm._state = dp.PlayerState.IDLE

window6._loaded_source = "/dev/sr0"
window6.status.setText("")
window6._disc_gone_streak = {}
dp.disc_present = lambda dev: False  # type: ignore[assignment]
window6._disc_presence = {"/dev/sr0": True}
window6._poll_disc_insertion()
window6._poll_disc_insertion()
check(window6.status.text() == "disc removed", "removal edge not reported")

# ── (m) seek bar, scrubbing signals and pressed-only preview ────────
scrub_osd = dp.Xbox360Osd()
scrub_osd.resize(1280, 64)
scrub_osd.set_transport(
    state=dp.PlayerState.PLAYING, time_pos=30.0, duration=120.0
)
check(abs(scrub_osd._seek_fraction() - 0.25) < 1e-9, "bar fraction 30/120")
_track, _hit = scrub_osd._seek_bar_rect()
check(abs(_track.left() - scrub_osd.LEFT_MARGIN) < 1e-9, "bar left margin")
check(abs(_track.width() - (1280 - 2 * scrub_osd.LEFT_MARGIN)) < 1e-9, "bar width")
check(abs(_track.height() - 6.0) < 1e-9, "bar is not 6 px tall")
check(abs(_hit.height() - 18.0) < 1e-9, "bar hit rect is not 18 px tall")
check(_hit.contains(_track.center()), "bar hit rect does not cover the track")
scrub_osd.set_transport(state=dp.PlayerState.PLAYING, time_pos=30.0, duration=None)
check(scrub_osd._seek_fraction() == 0.0, "no duration -> empty bar")
scrub_osd.set_transport(state=dp.PlayerState.PLAYING, time_pos=30.0, duration=0.0)
check(scrub_osd._seek_fraction() == 0.0, "zero duration -> empty bar")
scrub_osd.set_transport(state=dp.PlayerState.PLAYING, time_pos=None, duration=120.0)
check(scrub_osd._seek_fraction() == 0.0, "no time_pos -> empty bar")
scrub_osd.set_transport(
    state=dp.PlayerState.PLAYING, time_pos=30.0, duration=120.0
)

_scrub_events: list[tuple[str, float | None]] = []
scrub_osd.scrub_pressed.connect(lambda f: _scrub_events.append(("press", round(f, 4))))
scrub_osd.scrub_moved.connect(lambda f: _scrub_events.append(("move", round(f, 4))))
scrub_osd.scrub_released.connect(lambda: _scrub_events.append(("release", None)))


def _scrub_mouse(
    kind: QEvent.Type, x: float, y: float, held: bool
) -> QMouseEvent:
    return QMouseEvent(
        kind,
        QPointF(x, y),
        QPointF(x, y),
        Qt.MouseButton.LeftButton
        if kind != QEvent.Type.MouseMove
        else Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


_y = _hit.center().y()
_x25 = _track.left() + 0.25 * _track.width()
_x75 = _track.left() + 0.75 * _track.width()
scrub_osd.mousePressEvent(_scrub_mouse(QEvent.Type.MouseButtonPress, _x25, _y, True))
check(_scrub_events == [("press", 0.25)], f"press fraction -> {_scrub_events}")
scrub_osd.mouseMoveEvent(_scrub_mouse(QEvent.Type.MouseMove, _x75, _y, True))
check(_scrub_events[-1] == ("move", 0.75), f"move fraction -> {_scrub_events}")
scrub_osd.mouseReleaseEvent(_scrub_mouse(QEvent.Type.MouseButtonRelease, _x75, _y, False))
check(_scrub_events[-1] == ("release", None), "release signal missing")
check(len(_scrub_events) == 3, f"unexpected scrub trace {_scrub_events}")
# Moves outside a press are ignored (no stray preview/seek traffic).
scrub_osd.mouseMoveEvent(_scrub_mouse(QEvent.Type.MouseMove, _x25, _y, False))
check(len(_scrub_events) == 3, "move outside press emitted a scrub signal")

# Preview is stored, painted without crashing, and cleared on show/hide.
scrub_osd.show()
app.processEvents()
scrub_osd.set_preview(QImage(64, 36, QImage.Format.Format_RGB32))
check(scrub_osd._preview is not None, "set_preview did not store the image")
scrub_osd._scrub_x = 600.0
check(not scrub_osd.grab().isNull(), "preview paint produced a null pixmap")
scrub_osd.hide()
app.processEvents()
check(scrub_osd._preview is None, "hide did not clear the preview")
scrub_osd.set_preview(QImage(4, 4, QImage.Format.Format_RGB32))
scrub_osd.show_osd()
check(scrub_osd._preview is None, "show_osd did not clear the preview")
scrub_osd.hide()


class _ScrubPlayer:
    duration: float | None = 200.0
    time_pos: float | None = 10.0
    aid: object = None
    sid: object = None
    chapter: int | None = None
    chapter_list: list[int] = []
    volume: float = 100.0


scrub_engine = dp.DvdPlaybackEngine()
scrub_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
scrub_window = dp.MainWindow(scrub_engine)
scrub_window._auto_detected = True
scrub_window._attach_engine = lambda: None  # type: ignore[method-assign]
scrub_engine._player = _ScrubPlayer()
_seeks: list[float] = []
_captures: list[int] = []
scrub_engine.seek_to = lambda s: _seeks.append(float(s))  # type: ignore[method-assign]
scrub_engine.capture_frame = lambda: (  # type: ignore[attr-defined]
    _captures.append(1) or (2, 2, bytes([10, 20, 30, 0]) * 4)
)
_preview_calls: list[object] = []
_orig_preview = scrub_window.osd.set_preview
scrub_window.osd.set_preview = lambda img: (  # type: ignore[method-assign]
    _preview_calls.append(img),
    _orig_preview(img),
)[1]

scrub_window._on_scrub_pressed(0.25)
check(_seeks == [50.0], f"press seek -> {_seeks}")
check(len(_captures) == 1, "press did not capture a preview")
_preview = scrub_window.osd._preview
check(_preview is not None and not _preview.isNull(), "preview image not set")
check((_preview.width(), _preview.height()) == (2, 2), "preview image size")
scrub_window._on_scrub_moved(0.50)
check(_seeks == [50.0], f"move was not throttled -> {_seeks}")
check(len(_captures) == 1, "throttled move captured a preview")
# Still inside the 250 ms window: no seek.
scrub_window._scrub_last_seek -= 0.2
scrub_window._on_scrub_moved(0.50)
check(_seeks == [50.0], f"move inside 250 ms window seeked -> {_seeks}")
# Outside the seek window but inside the 200 ms capture window: seek only.
scrub_window._scrub_last_seek -= 0.1
scrub_window._on_scrub_moved(0.50)
check(_seeks == [50.0, 100.0], f"throttled move seek -> {_seeks}")
check(len(_captures) == 1, "capture ignored the lazy 200 ms guard")
# Both guards elapsed: seek plus one lazy capture.
scrub_window._scrub_last_seek -= 0.3
scrub_window._scrub_last_capture -= 0.2
scrub_window._on_scrub_moved(0.75)
check(_seeks == [50.0, 100.0, 150.0], f"second throttled move -> {_seeks}")
check(len(_captures) == 2, "lazy capture did not refresh the preview")
scrub_window._on_scrub_released()
check(_seeks == [50.0, 100.0, 150.0, 150.0], f"release final seek -> {_seeks}")
check(len(_captures) == 2, "release captured a preview")
check(scrub_window.osd._preview is None, "release did not clear the preview")
check(_preview_calls[-1] is None, "final set_preview was not None")
check(not scrub_window._scrub_active and scrub_window._scrub_fraction is None, "release did not reset scrub state")
check(scrub_window._scrub_last_seek == 0.0, "release did not reset the throttle")
check(scrub_window._scrub_last_capture == 0.0, "release did not reset the capture guard")
_non_none = [call for call in _preview_calls if call is not None]
check(len(_non_none) == 2, f"preview set outside scrub pairs: {_preview_calls}")
# Moves after release never seek or touch the preview.
scrub_window._on_scrub_moved(0.9)
check(_seeks == [50.0, 100.0, 150.0, 150.0], "move after release seeked")
check(len(_preview_calls) == 3, "move after release changed the preview")

# ── (l2) C4 seek atomicity: rapid burst keeps only press + final ────
# A 150 ms burst is entirely inside the GUI's 250 ms throttle: the GUI
# fires no coalescing timer of its own; it forwards the press, drops the
# intermediate moves, then forwards EXACTLY ONE final seek on release.
check(not hasattr(scrub_window, "_scrub_timer"),
      "GUI installed a coalescing timer instead of trusting the engine")
_seeks.clear()
_captures.clear()
_preview_calls.clear()
scrub_window._on_scrub_pressed(0.10)
burst_start = time.monotonic()
for step in range(1, 11):
    scrub_window._on_scrub_moved(step / 10.0)
check(time.monotonic() - burst_start < 0.15, "burst exceeded 150 ms")
seeks_before_release = list(_seeks)
scrub_window._on_scrub_released()
check(_seeks == [20.0, 200.0],
      f"burst did not collapse to press + final: {_seeks}")
check(len(_seeks) == len(seeks_before_release) + 1,
      "release issued more than one final seek")
check(_seeks[-1] == 200.0, f"release final fraction: {_seeks[-1]}")
check(scrub_window.osd._preview is None, "burst release kept the preview")
check(not scrub_window._scrub_active
      and scrub_window._scrub_fraction is None
      and scrub_window._scrub_last_seek == 0.0
      and scrub_window._scrub_last_capture == 0.0,
      "burst release did not reset throttle/scrub state")

# Latest-wins pass-through: when moves clear the throttle window, EVERY
# move goes to engine.seek_to in order (the engine coalesces, not the GUI).
_seeks.clear()
_preview_calls.clear()
scrub_window._on_scrub_pressed(0.0)
for step in range(1, 11):
    scrub_window._scrub_last_seek -= 1.0  # pretend > 250 ms between moves
    scrub_window._on_scrub_moved(step / 10.0)
expected = [0.0] + [step * 20.0 for step in range(1, 11)]
check(_seeks == expected, f"latest-wins pass-through: {_seeks}")
before = len(_seeks)
scrub_window._on_scrub_released()
check(_seeks == expected + [200.0], f"latest-wins release: {_seeks}")
check(len(_seeks) == before + 1, "latest-wins release seek was not singular")
check(_seeks[-1] == 200.0, "latest-wins final fraction")
check(scrub_window.osd._preview is None
      and scrub_window._scrub_fraction is None
      and scrub_window._scrub_last_seek == 0.0,
      "latest-wins release did not clear preview/state")

# duration None: scrubbing is a no-op and no preview appears.
scrub_engine._player = None
_seeks.clear()
_captures.clear()
_preview_calls.clear()
scrub_window._on_scrub_pressed(0.5)
scrub_window._on_scrub_released()
check(_seeks == [] and _captures == [], "scrub without duration touched the engine")
check(all(call is None for call in _preview_calls), "preview without duration")

# Absent capture_frame: scrubbing still seeks, preview is skipped.
scrub_engine._player = _ScrubPlayer()
del scrub_engine.capture_frame  # type: ignore[attr-defined]
_seeks.clear()
_preview_calls.clear()
scrub_window._on_scrub_pressed(0.5)
check(_seeks == [100.0], f"seek without capture_frame -> {_seeks}")
check(scrub_window.osd._preview is None, "missing capture_frame still set a preview")
scrub_window._on_scrub_released()

# Render mode: the big live thumbnail comes from grabFramebuffer(), never
# from engine.capture_frame(), and carries the frame timestamp.
scrub_engine.capture_frame = lambda: (  # type: ignore[attr-defined]
    _captures.append(1) or (2, 2, bytes([10, 20, 30, 0]) * 4)
)
_render_image = QImage(640, 360, QImage.Format.Format_RGB32)
_render_image.fill(QColor(10, 20, 30))
_grab_calls: list[int] = []


def _fake_grab() -> QImage:
    _grab_calls.append(1)
    return _render_image


scrub_window._render_mode = True
scrub_window.resize(1280, 720)
scrub_window.video.grabFramebuffer = _fake_grab  # type: ignore[method-assign]
_seeks.clear()
_captures.clear()
_preview_calls.clear()
scrub_window._on_scrub_pressed(0.25)
check(_seeks == [50.0], f"render press seek -> {_seeks}")
check(_grab_calls == [1], "render press did not grab the GL framebuffer")
check(_captures == [], "render mode fell back to capture_frame")
check(_preview_calls[-1] is not None, "render grab did not reach set_preview")
_render_preview = scrub_window.osd._preview
check(
    _render_preview is not None
    and (_render_preview.width(), _render_preview.height()) == (640, 360),
    "render preview pixels are not the GL framebuffer image",
)
scrub_window._on_scrub_moved(0.5)
check(_grab_calls == [1], "throttled move grabbed a frame")
scrub_window._scrub_last_seek -= 0.3
scrub_window._scrub_last_capture -= 0.2
scrub_window._on_scrub_moved(0.5)
check(_seeks == [50.0, 100.0], f"render throttled seek -> {_seeks}")
check(_grab_calls == [1, 1], "render throttle did not grab after the seek")
check(_preview_calls[-1] is not None, "throttled render grab missed set_preview")
check(scrub_window.osd._preview_seconds == 100.0, "preview timestamp not recorded")
check(scrub_window.osd._preview_rect().width() >= 320.0, "thumbnail width < 320")
check(scrub_window.osd._preview_rect().height() >= 180.0, "thumbnail height < 180")
check(
    scrub_window.scrub_preview._preview_rect().width() >= 320.0
    and scrub_window.scrub_preview._preview_rect().height() >= 180.0,
    "overlay thumbnail smaller than 320x180",
)
check(scrub_window.scrub_preview.timestamp_text() == "01:40", "timestamp format")
check(not scrub_window.scrub_preview._image is None, "overlay did not get the frame")
scrub_window._on_scrub_released()
check(scrub_window.osd._preview is None, "render release kept the preview")
check(scrub_window.scrub_preview._image is None, "render release kept overlay preview")
check(
    scrub_window.osd._preview_seconds is None,
    "render release kept the preview timestamp",
)

# Loki release race: a release DURING grabFramebuffer must not resurrect
# the preview after the capture returns.
_release_grabs: list[int] = []


def _releasing_grab() -> QImage:
    _release_grabs.append(1)
    scrub_window._on_scrub_released()
    return _render_image


scrub_window.video.grabFramebuffer = _releasing_grab  # type: ignore[method-assign]
_seeks.clear()
_preview_calls.clear()
scrub_window._on_scrub_pressed(0.25)
check(_release_grabs == [1], "release-race grab did not run")
check(scrub_window.osd._preview is None, "release during grab resurrected preview")
check(
    scrub_window.scrub_preview._image is None,
    "release during grab resurrected overlay preview",
)
check(
    _preview_calls and _preview_calls[-1] is None,
    "release during grab ended with a non-None preview",
)
check(
    not scrub_window._scrub_active
    and scrub_window._scrub_fraction is None
    and scrub_window._scrub_last_capture == 0.0,
    "release during grab did not reset scrub state",
)
scrub_window._render_mode = False
scrub_engine._player = None
scrub_window.close()

# ── (m) OSD language readout is painted ────────────────────────────
osd_lang = dp.Xbox360Osd()
osd_lang.resize(1280, 64)
osd_lang.set_transport(
    state=dp.PlayerState.PLAYING, audio_lang="en", subtitle_lang="fr"
)
image_with = osd_lang.grab().toImage()
osd_lang.set_transport(
    state=dp.PlayerState.PLAYING, audio_lang=None, subtitle_lang=None
)
image_without = osd_lang.grab().toImage()
check(not image_with.isNull() and not image_without.isNull(), "language grab failed")
check(image_with != image_without, "language readout did not change the painted bar")
check(osd_lang._track_text() is None, "empty track text not omitted")

# ── (n) A3: HomeMenu overlay + key routing keeps playback alive ─────
home_engine = dp.DvdPlaybackEngine()
home_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
home_window = dp.MainWindow(home_engine)
home_window._auto_detected = True
home_window._attach_engine = lambda: None  # type: ignore[method-assign]
home_window.show()
app.processEvents()
home_calls: list[tuple[str, tuple[object, ...]]] = []
for _name in ("play_pause", "stop", "menu_nav", "menu_select", "set_speed"):
    setattr(
        home_engine,
        _name,
        lambda *a, _n=_name: home_calls.append((_n, a)),
    )
dp.list_dvd_devices = lambda: ["/dev/sr0"]  # type: ignore[assignment]
dp.disc_present = lambda dev: True  # type: ignore[assignment]
home_engine._sm._state = dp.PlayerState.PLAYING
home_window.home.hide()
home_window.osd.hide()
home_window._open_home()
check(not home_window.home.isHidden(), "G did not open HomeMenu")
check(home_engine.state is dp.PlayerState.PLAYING,
      "opening HomeMenu interrupted playback")
check(home_calls == [], f"HomeMenu touched playback: {home_calls}")
check(home_window.home.blade_name() == "DVD", "HomeMenu did not start on DVD")
check([payload for _text, payload in home_window.home.entries()] == ["/dev/sr0"],
      "DVD blade entries are not list_dvd_devices()")

# Blades and entries navigate; entering HomeMenu captures keys.
home_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right.value,
              Qt.KeyboardModifier.NoModifier)
)
check(home_window.home.blade_name() == "USB", "Right did not switch blade")
home_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left.value,
              Qt.KeyboardModifier.NoModifier)
)
check(home_window.home.blade_name() == "DVD", "Left did not switch blade")
home_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape.value,
              Qt.KeyboardModifier.NoModifier)
)
check(home_window.home.isHidden(), "Escape did not close HomeMenu")
check(home_calls == [], f"Esc leaked engine actions: {home_calls}")

# G opens anywhere; M toggles the OSD independently of HomeMenu.
home_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_G.value,
              Qt.KeyboardModifier.NoModifier)
)
check(not home_window.home.isHidden(), "G did not open HomeMenu")
home_window.osd.hide()
home_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_M.value,
              Qt.KeyboardModifier.NoModifier)
)
check(home_window.osd.isVisible(), "M did not show the OSD over HomeMenu")
check(not home_window.home.isHidden(), "M closed HomeMenu")
home_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_M.value,
              Qt.KeyboardModifier.NoModifier)
)
check(not home_window.osd.isVisible(), "M did not hide the OSD over HomeMenu")
home_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape.value,
              Qt.KeyboardModifier.NoModifier)
)
check(home_window.home.isHidden(), "Esc did not close HomeMenu again")
check(home_calls == [], f"overlay key routing leaked engine actions: {home_calls}")

# A2: a slow PVD label read runs on a daemon thread, never blocks the UI.
home_menu = dp.HomeMenu()
home_menu.resize(1280, 720)
_orig_label = getattr(dp.DiscIdentity, "label")
_label_calls: list[str] = []


def _slow_label(device: str) -> str:
    _label_calls.append(device)
    time.sleep(2.0)
    return "SLOW-DISC"


dp.DiscIdentity.label = staticmethod(_slow_label)  # type: ignore[attr-defined]
_ticks: list[int] = []
_ticker = dp.QTimer()
_ticker.setInterval(20)
_ticker.timeout.connect(lambda: _ticks.append(1))
_ticker.start()
_started = time.monotonic()
home_menu.refresh(["/dev/sr0"], {"/dev/sr0": True})
_elapsed = time.monotonic() - _started
check(_elapsed < 0.5, f"label refresh blocked the UI for {_elapsed:.2f}s")
check(_label_calls == ["/dev/sr0"], "label reader did not start")
_deadline = time.monotonic() + 3.0
while time.monotonic() < _deadline and "SLOW-DISC" not in home_menu._labels.values():
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
_ticker.stop()
check(len(_ticks) > 0, "event loop was not responsive during the label read")
check(home_menu._labels.get("/dev/sr0") == "SLOW-DISC",
      "slow label never reached HomeMenu")
dp.DiscIdentity.label = staticmethod(_orig_label)  # type: ignore[attr-defined]
home_menu.deleteLater()

# ── (p) D1: the active DVD blade paints Xbox green on open ─────────
launch_menu = dp.HomeMenu()
check(launch_menu._blade == 0, "HomeMenu did not start on the DVD blade")
check(launch_menu._selection == 0, "HomeMenu did not start on the first entry")
launch_menu.resize(1280, 720)
launch_menu._dvd_devices = ["/dev/sr0"]
launch_menu._dvd_present = {"/dev/sr0": True}
check(len(launch_menu.entries()) == 1, "active DVD blade has no entries")
launch_image = QImage(1280, 720, QImage.Format.Format_ARGB32)
launch_image.fill(QColor(0, 0, 0))
launch_menu.render(launch_image)
green_pixels = 0
for _y in range(launch_image.height()):
    for _x in range(launch_image.width()):
        _px = launch_image.pixelColor(_x, _y)
        if (
            abs(_px.red() - 116) <= 30
            and abs(_px.green() - 195) <= 30
            and abs(_px.blue() - 41) <= 30
        ):
            green_pixels += 1
check(green_pixels > 100,
      f"active blade is not green at open: {green_pixels} px")
launch_menu.deleteLater()

# Enter is edge-triggered: a rapid double press selects exactly once, so
# one blade press can never re-enter the load flow while LOADING.
dedupe_menu = dp.HomeMenu()
import dvd_player.window as _window_mod  # noqa: E402
dedupe_menu._dvd_devices = ["/dev/sr0"]
dedupe_menu._dvd_present = {"/dev/sr0": True}
_picked: list[str] = []
dedupe_menu.entry_selected.connect(_picked.append)
dedupe_menu.handle_key(Qt.Key.Key_Return.value)
dedupe_menu.handle_key(Qt.Key.Key_Return.value)
check(_picked == ["/dev/sr0"], f"double Enter selected {_picked}")
check(_window_mod.wid_embedding_safe() is True,
      "wid path wrongly rejected on a non-Wayland platform")
dedupe_menu.resize(1280, 720)
rows = dedupe_menu.entry_rows()
check(len(rows) == 1 and rows[0].title == "DVD video",
      f"DVD row title -> {rows}")
check(rows[0].subtitle == "/dev/sr0", f"DVD row subtitle -> {rows[0].subtitle}")
dedupe_menu._dvd_present = {"/dev/sr0": False}
empty_row = dedupe_menu.entry_rows()[0]
check(empty_row.title == "No disc" and empty_row.subtitle == "/dev/sr0",
      f"empty disc row -> {empty_row}")
dedupe_menu._dvd_devices = []
dedupe_menu._dvd_present = {}
check(dedupe_menu.empty_state_text() == "No disc — insert one",
      f"DVD empty state -> {dedupe_menu.empty_state_text()!r}")
check("automatic" in dedupe_menu.empty_state_hint(),
      "empty state lost its guidance line")
check(dedupe_menu.footer_text() ==
      "Enter select  ·  ←/→ blades  ·  Esc back  ·  Ctrl+H help",
      f"footer text -> {dedupe_menu.footer_text()!r}")
footer = dedupe_menu.footer_rect()
check(footer.height() > 0 and footer.bottom() <= 720,
      f"footer band outside the menu -> {footer}")
dedupe_menu._blade = 2
storage_row = dedupe_menu.entry_rows()[0]
check(storage_row.subtitle == "Choose a VIDEO_TS folder or .iso"
      and storage_row.payload == "", f"storage row -> {storage_row}")
dedupe_menu.deleteLater()

# ── (o) C1/C2: ChapterGrid fidelity + burst coalescing ─────────────
class _ChapterPlayer:
    time_pos: float | None = 0.0
    duration: float | None = 900.0
    aid: object = None
    sid: object = None
    chapter: int | None = 1
    chapter_list: list[dict[str, float]] = [
        {"time": 0.0},
        {"time": 100.0},
        {"time": 250.0},
        {"time": 400.0},
    ]
    volume: float = 100.0

    def terminate(self) -> None:
        pass


grid_engine = dp.DvdPlaybackEngine()
grid_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
grid_window = dp.MainWindow(grid_engine)
grid_window._auto_detected = True
grid_window._attach_engine = lambda: None  # type: ignore[method-assign]
grid_engine._player = _ChapterPlayer()
grid_engine._sm._state = dp.PlayerState.PLAYING
grid_seeks: list[float] = []
grid_engine.seek_to = lambda seconds: grid_seeks.append(float(seconds))  # type: ignore[method-assign]
grid_window.home.hide()
grid_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C.value,
              Qt.KeyboardModifier.NoModifier)
)
check(not grid_window.chapter_grid.isHidden(), "C did not open the ChapterGrid")
check(grid_window.chapter_grid.chapters() == [0, 100, 250, 400],
      "grid chapters do not match engine.chapter_list")
check(len(grid_window.chapter_grid._tile_rects()) == 4,
      "tile count does not match chapter count")
# C1: Enter chooses after the 350 ms settle (one seek).
_enter = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return.value,
                   Qt.KeyboardModifier.NoModifier)
grid_window.keyPressEvent(_enter)
check(grid_seeks == [], "grid Enter seeked before the settle timer")
_deadline = time.monotonic() + 1.0
while time.monotonic() < _deadline and not grid_seeks:
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
check(grid_seeks == [0.0], f"C1 Enter seek -> {grid_seeks}")
check(grid_window.chapter_grid.isHidden(), "grid stayed open after choosing")

# C2: a burst of arrows/Enter coalesces into ONE final seek.
grid_seeks.clear()
grid_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C.value,
              Qt.KeyboardModifier.NoModifier)
)
check(not grid_window.chapter_grid.isHidden(), "C did not reopen the grid")
for _ in range(3):
    for _key in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Down,
                 Qt.Key.Key_Return):
        grid_window.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, _key.value,
                      Qt.KeyboardModifier.NoModifier)
        )
check(grid_seeks == [], "burst leaked seeks before settling")
_deadline = time.monotonic() + 1.0
while time.monotonic() < _deadline and not grid_seeks:
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
check(len(grid_seeks) == 1, f"burst coalesced into {len(grid_seeks)} seeks: {grid_seeks}")
_final_chapter = [0, 100, 250, 400][grid_window.chapter_grid.selection()]
check(grid_seeks == [float(_final_chapter)],
      f"burst did not honour the final tile: {grid_seeks}")

# Closing during a burst cancels the pending seek; nothing escapes after.
grid_seeks.clear()
grid_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C.value,
              Qt.KeyboardModifier.NoModifier)
)
grid_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return.value,
              Qt.KeyboardModifier.NoModifier)
)
grid_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape.value,
              Qt.KeyboardModifier.NoModifier)
)
deadline = time.monotonic() + 0.6
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
check(grid_seeks == [], f"seek escaped after grid close: {grid_seeks}")
check(grid_window.chapter_grid.isHidden(), "grid did not close on Escape")

# ── (p) G1/G2: MPRIS registration + mapping with a fake bus ────────
class _FakeBus:
    def __init__(self) -> None:
        self.connected = True
        self.services: list[str] = []
        self.objects: list[tuple[str, object]] = []
        self.unregistered: list[str] = []

    def isConnected(self) -> bool:  # noqa: N802 - Qt API name
        return self.connected

    def registerService(self, name: str) -> bool:  # noqa: N802
        self.services.append(name)
        return True

    def registerObject(self, path: str, obj: object, options: object) -> bool:  # noqa: N802
        del options
        self.objects.append((path, obj))
        return True

    def unregisterObject(self, path: str) -> None:  # noqa: N802
        self.unregistered.append(path)

    def unregisterService(self, name: str) -> None:  # noqa: N802
        self.unregistered.append(name)


mpris_engine = dp.DvdPlaybackEngine()
mpris_calls: list[tuple[str, tuple[object, ...]]] = []
for _name in ("play_pause", "stop", "next_chapter", "previous_chapter",
              "seek_to", "adjust_volume"):
    setattr(
        mpris_engine,
        _name,
        lambda *a, _n=_name: mpris_calls.append((_n, a)),
    )
fake_bus = _FakeBus()
mpris = dp.MprisService(mpris_engine, bus=fake_bus)
check(mpris.start() is True, "MPRIS start failed with an injected bus")
check(dp.MprisService.SERVICE_NAME in fake_bus.services,
      "MPRIS service name was not registered")
check(any(path == dp.MprisService.OBJECT_PATH for path, _ in fake_bus.objects),
      "MPRIS object path was not exported")
check(mpris.registered is True, "MPRIS did not report registration")
check(mpris.start() is True, "duplicate MPRIS registration was fatal")
mpris_engine._sm._state = dp.PlayerState.PLAYING
check(mpris.PlaybackStatus() == "Playing", "PLAYING did not map to Playing")
mpris_engine._sm._state = dp.PlayerState.PAUSED
check(mpris.PlaybackStatus() == "Paused", "PAUSED did not map to Paused")
mpris_engine._sm._state = dp.PlayerState.STOPPED
check(mpris.PlaybackStatus() == "Stopped", "STOPPED did not map to Stopped")

mpris_calls.clear()
mpris.PlayPause()
mpris.Stop()
mpris.Next()
mpris.Previous()
check(mpris_calls == [
    ("play_pause", ()),
    ("stop", ()),
    ("next_chapter", ()),
    ("previous_chapter", ()),
], f"MPRIS transport mapping -> {mpris_calls}")
mpris_calls.clear()
mpris_engine._sm._state = dp.PlayerState.PAUSED
mpris.Play()
check(mpris_calls == [("play_pause", ())], f"MPRIS Play -> {mpris_calls}")
mpris_calls.clear()
mpris_engine._sm._state = dp.PlayerState.PLAYING
mpris.Pause()
check(mpris_calls == [("play_pause", ())], f"MPRIS Pause -> {mpris_calls}")

mpris_engine._player = _ChapterPlayer()
mpris_engine._player.time_pos = 100.0
mpris_calls.clear()
mpris.Seek(2_500_000)
check(mpris_calls == [("seek_to", (102.5,))], f"MPRIS Seek -> {mpris_calls}")
mpris_calls.clear()
mpris.SetPosition(30_000_000)
check(mpris_calls == [("seek_to", (30.0,))], f"MPRIS SetPosition -> {mpris_calls}")
mpris_calls.clear()
mpris_engine._player.volume = 50.0
mpris.SetVolume(70.0)
check(mpris_calls == [("adjust_volume", (20.0,))],
      f"MPRIS SetVolume -> {mpris_calls}")
mpris_engine.position_changed.emit(42.0)
check(abs(mpris._position - 42.0) < 1e-9,
      "position_changed did not update the MPRIS position")
check(mpris._position_timer.interval() == 1000,
      "MPRIS position timer is not 1 s")
check(mpris.Metadata() == {"xesam:title": "DVD", "xesam:artist": ["DVD"]},
      f"default metadata -> {mpris.Metadata()}")
mpris_engine._disc_label = "INTERSTELLAR"
check(mpris.Metadata()["xesam:title"] == "INTERSTELLAR",
      "metadata did not use engine.disc_label")
grid_engine._disc_label = "INTERSTELLAR"
grid_window._update_window_title()
check(grid_window.windowTitle() == "DVD Player — INTERSTELLAR",
      f"window title -> {grid_window.windowTitle()!r}")
mpris.stop()
check(dp.MprisService.SERVICE_NAME in fake_bus.unregistered,
      "MPRIS stop did not release the service")
check(dp.MprisService.OBJECT_PATH in fake_bus.unregistered,
      "MPRIS stop did not release the object")

# MainWindow exposes the start/stop hooks used by main().
check(callable(getattr(grid_window, "start_mpris", None)),
      "MainWindow.start_mpris missing")
check(callable(getattr(grid_window, "stop_mpris", None)),
      "MainWindow.stop_mpris missing")
grid_window.stop_mpris()
check(grid_window._mpris_started is False, "stop_mpris left the service started")
mpris_engine.dispose()

# ── (q) Ctrl+H help overlay + README keybinding coverage ───────────
home_calls.clear()
home_window.home.hide()
home_window.osd.hide()
home_window.help.hide()
home_engine._sm._state = dp.PlayerState.PLAYING
_ctrl_h = QKeyEvent(
    QEvent.Type.KeyPress,
    Qt.Key.Key_H.value,
    Qt.KeyboardModifier.ControlModifier,
)
home_window.keyPressEvent(_ctrl_h)
check(not home_window.help.isHidden(), "Ctrl+H did not open HelpOverlay")
check(home_window.home.isHidden(), "Ctrl+H left HomeMenu visible")
check(home_window.osd.isHidden(), "Ctrl+H left the OSD visible")
check(home_engine.state is dp.PlayerState.PLAYING,
      "Ctrl+H paused playback")
check(home_calls == [], f"Ctrl+H touched playback: {home_calls}")
home_window.help.resize(1280, 720)
check(not home_window.help.grab().isNull(), "HelpOverlay paint failed")
home_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape.value,
              Qt.KeyboardModifier.NoModifier)
)
check(home_window.help.isHidden(), "Esc did not close HelpOverlay")
check(home_engine.state is dp.PlayerState.PLAYING,
      "Esc from help paused playback")
home_window.keyPressEvent(_ctrl_h)
check(not home_window.help.isHidden(), "Ctrl+H did not reopen HelpOverlay")
home_window.keyPressEvent(_ctrl_h)
check(home_window.help.isHidden(), "Ctrl+H did not close HelpOverlay")
check(home_calls == [], f"help toggle touched playback: {home_calls}")

_required_keys = {
    "↑", "↓", "←", "→", "Enter", "Backspace", "X", "Y", "Space", "S",
    "F", "R", "N", "P", "I", "O", "+", "-", "E", "M", "G", "C", "A",
    "Ctrl+F", "Ctrl+L", "Ctrl+H", "Esc",
}
_listed = {
    token for key, _action in dp.HelpOverlay.BINDINGS for token in key.split()
}
check(_required_keys <= _listed, f"help misses keys: {sorted(_required_keys - _listed)}")
check(dp.HelpOverlay.KEYBINDINGS == dp.HelpOverlay.BINDINGS,
      "HelpOverlay.KEYBINDINGS alias drifted")
_readme = open(
    "/home/giancarlo/projects/playground/CD_Player/README.md",
    encoding="utf-8",
).read()
for _keys, _action in dp.HelpOverlay.bindings():
    for _token in _keys.split():
        check(_token in _readme,
              f"README keybindings table misses {_token!r} ({_action})")

home_window.close()
grid_window.close()

# ── idle cursor auto-hide (feat/cursor-autohide) ────────────────────
# Fake-clock style: drive the single-shot timeout by emitting its signal
# instead of sleeping 5 s. Mouse events are delivered through Qt notify so
# the window-wide application filter is exercised (OSD is a descendant).
_cursor_engine = dp.DvdPlaybackEngine()
_cursor_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
_cursor_window = dp.MainWindow(_cursor_engine)
_cursor_window._auto_detected = True
_cursor_window.resize(640, 360)
_cursor_window.show()
app.processEvents()
check(_cursor_window._cursor_timer.isSingleShot(), "cursor timer not single-shot")
check(_cursor_window._cursor_timer.interval() == 5000, "cursor timer is not 5 s")
check(_cursor_window._cursor_timer.isActive(), "cursor timer not running")


def _move_event() -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(11, 9),
        QPointF(11, 9),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


_cursor_window._cursor_timer.stop()
QApplication.sendEvent(_cursor_window.osd, _move_event())
app.processEvents()
check(_cursor_window._cursor_timer.isActive(), "OSD mouse move did not restart timer")

# Timeout blanks the cursor; no OSD flash from timer activity.
_cursor_window.osd.hide()
_cursor_window._cursor_timer.timeout.emit()
check(_cursor_window.cursor().shape() == Qt.CursorShape.BlankCursor,
      "idle cursor not hidden after timeout")
check(not _cursor_window.osd.isVisible(), "cursor timeout flashed the OSD")

# Any movement restores instantly and restarts the countdown.
QApplication.sendEvent(_cursor_window.video, _move_event())
app.processEvents()
check(_cursor_window.cursor().shape() != Qt.CursorShape.BlankCursor,
      "movement did not restore the cursor")
check(_cursor_window._cursor_timer.isActive(), "movement did not restart timer")

# Never hide while scrubbing (mouse held on the seek bar).
_cursor_window._on_scrub_pressed(0.5)
_cursor_window._cursor_timer.timeout.emit()
check(_cursor_window.cursor().shape() != Qt.CursorShape.BlankCursor,
      "cursor hidden during an active scrub")
_cursor_window._on_scrub_released()

# Never hide while a modal dialog is up; the refusal re-arms the timer.
_orig_modal = QApplication.activeModalWidget
QApplication.activeModalWidget = staticmethod(lambda: object())  # type: ignore[method-assign]
try:
    _cursor_window._cursor_timer.stop()
    _cursor_window._cursor_timer.timeout.emit()
    check(_cursor_window.cursor().shape() != Qt.CursorShape.BlankCursor,
          "cursor hidden over a modal dialog")
    check(_cursor_window._cursor_timer.isActive(),
          "modal guard disarmed the idle timer")
finally:
    QApplication.activeModalWidget = _orig_modal  # type: ignore[method-assign]

# Never hide under a live boot/loading splash; dismissing it must not leave
# a blank pointer and must leave the countdown armed.
_cursor_window.splash.show_boot()
app.processEvents()
check(_cursor_window.splash.isVisible(), "splash not visible for cursor guard")
_cursor_window._cursor_timer.stop()
_cursor_window._cursor_timer.timeout.emit()
check(_cursor_window.cursor().shape() != Qt.CursorShape.BlankCursor,
      "cursor hidden under a live splash")
check(_cursor_window._cursor_timer.isActive(),
      "splash guard disarmed the idle timer")
_cursor_window._dismiss_splash()
app.processEvents()
check(_cursor_window.cursor().shape() != Qt.CursorShape.BlankCursor,
      "splash dismissal left the cursor blank")
check(_cursor_window._cursor_timer.isActive(),
      "splash dismissal left the idle timer stopped")

# Close stops the timer and restores the cursor.
_cursor_window._cursor_timer.timeout.emit()
check(_cursor_window.cursor().shape() == Qt.CursorShape.BlankCursor,
      "cursor not hidden before close")
_cursor_window.close()
app.processEvents()
check(not _cursor_window._cursor_timer.isActive(), "cursor timer survived close")
check(_cursor_window.cursor().shape() != Qt.CursorShape.BlankCursor,
      "close left the cursor blank")

# ── fullscreen Esc hides OSD and cursor (feat/cursor-fullscreen) ─────
_fs_engine = dp.DvdPlaybackEngine()
_fs_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
_fs_window = dp.MainWindow(_fs_engine)
_fs_window._auto_detected = True
_fs_window.resize(640, 360)
_fs_window.showFullScreen()
app.processEvents()
check(_fs_window.isFullScreen(), "window did not enter fullscreen")


def _escape_event() -> QKeyEvent:
    return QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Escape.value,
        Qt.KeyboardModifier.NoModifier,
    )


# Idle timeout must blank the GL/video, OSD and home surfaces too, not just
# the top-level window (the reported bug: the pointer stayed an arrow).
_fs_window.osd.hide()
_fs_window._cursor_timer.stop()
_fs_window._cursor_timer.timeout.emit()
for _surface in (_fs_window, _fs_window.video, _fs_window.osd, _fs_window.home):
    check(_surface.cursor().shape() == Qt.CursorShape.BlankCursor,
          f"idle hide missed surface {type(_surface).__name__}")
check(QApplication.overrideCursor() is not None,
      "idle hide installed no application override cursor")
QApplication.sendEvent(_fs_window.video, _move_event())
app.processEvents()
for _surface in (_fs_window, _fs_window.video, _fs_window.osd, _fs_window.home):
    check(_surface.cursor().shape() != Qt.CursorShape.BlankCursor,
          f"movement did not restore surface {type(_surface).__name__}")
check(QApplication.overrideCursor() is None,
      "movement left the application override cursor installed")

# Esc in fullscreen hides the OSD, blanks the cursor at once and leaves the
# pointer hidden until movement (movement also reveals the OSD again).
_fs_window.osd.show_osd()
app.processEvents()
check(_fs_window.osd.isVisible(), "OSD not visible before fullscreen Esc")
_fs_window.keyPressEvent(_escape_event())
check(not _fs_window.osd.isVisible(), "fullscreen Esc did not hide the OSD")
for _surface in (_fs_window, _fs_window.video, _fs_window.osd, _fs_window.home):
    check(_surface.cursor().shape() == Qt.CursorShape.BlankCursor,
          f"fullscreen Esc did not blank surface {type(_surface).__name__}")
QApplication.sendEvent(_fs_window.video, _move_event())
app.processEvents()
for _surface in (_fs_window, _fs_window.video, _fs_window.osd, _fs_window.home):
    check(_surface.cursor().shape() != Qt.CursorShape.BlankCursor,
          f"movement after fullscreen Esc did not restore {type(_surface).__name__}")
check(_fs_window.osd.isVisible(), "movement after fullscreen Esc did not reveal OSD")

# Normal mode keeps the old contract: Esc hides only the OSD.
_fs_window.showNormal()
app.processEvents()
QApplication.sendEvent(_fs_window.video, _move_event())
app.processEvents()
_fs_window.osd.show_osd()
app.processEvents()
_fs_window.keyPressEvent(_escape_event())
check(not _fs_window.osd.isVisible(), "normal Esc did not hide the OSD")
check(_fs_window.cursor().shape() != Qt.CursorShape.BlankCursor,
      "normal-mode Esc blanked the cursor")

# Close while hidden restores every surface and drops the override cursor.
_fs_window._cursor_timer.stop()
_fs_window._hide_idle_cursor()
check(_fs_window.cursor().shape() == Qt.CursorShape.BlankCursor,
      "cursor not hidden before fullscreen close")
_fs_window.close()
app.processEvents()
for _surface in (_fs_window, _fs_window.video, _fs_window.osd, _fs_window.home):
    check(_surface.cursor().shape() != Qt.CursorShape.BlankCursor,
          f"close left surface {type(_surface).__name__} blank")
check(QApplication.overrideCursor() is None,
      "close left the application override cursor installed")

dp.ResumeStore.get = _orig_get
dp.check_environment = _orig_env
dp.list_dvd_devices = _orig_devices
dp.disc_present = _orig_present

window.close()
window2.close()
window4.close()
window5.close()
window6.close()
app.processEvents()

# ── (z) render-api surface: GL widget, mouse coords, overlay stacking ─
# Documented test-edits for W2 (R6/R7): the render mode swaps in a
# QOpenGLWidget while the wid path keeps the old plain widget.
from dvd_player import window as _winmod  # noqa: E402

check(not isinstance(window.video, _winmod.VideoGLWidget),
      "wid window must not use the GL surface")
check(_winmod.render_mode_active(dp.DvdPlaybackEngine()) is False,
      "render mode active without an opt-in")
check(_winmod.render_mode_active(object(), {"DVD_PLAYER_RENDER_MODE": "render"}),
      "env render mode was not detected")
check(not _winmod.render_mode_active(object(), {}),
      "empty env wrongly selected render mode")

render_engine = dp.DvdPlaybackEngine()
render_engine.render_mode = "render"  # type: ignore[attr-defined]
render_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
gl_window = dp.MainWindow(render_engine)
gl_window._auto_detected = True
check(isinstance(gl_window.video, _winmod.VideoGLWidget),
      "render mode did not build VideoGLWidget")
check(gl_window.video.parentWidget() is not gl_window,
      "GL surface must sit inside the central widget, below the overlays")

# R6: widget-space coordinates scale by the device pixel ratio.
render_engine._menu_active = True
render_calls: list[tuple[str, tuple[object, ...]]] = []
render_engine.menu_mouse_move = (  # type: ignore[method-assign]
    lambda x, y: render_calls.append(("menu_mouse_move", (x, y)))
)
gl_window.video.devicePixelRatioF = lambda: 2.0  # type: ignore[method-assign]
gl_window._on_video_mouse(
    gl_window.video,
    QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(5, 7),
        QPointF(5, 7),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    ),
)
check(render_calls == [("menu_mouse_move", (10, 14))],
      f"GL mouse coords not DPR-scaled: {render_calls}")

# Render hooks are optional and guarded; recorders prove the wiring.
render_log: list[tuple[object, ...]] = []
render_engine.attach_render = (  # type: ignore[method-assign]
    lambda proc, fbo=None: (render_log.append(("attach", proc, fbo)), True)[1]
)
render_engine.update = lambda: render_log.append(("update",))  # type: ignore[method-assign]
render_engine.render_frame = (  # type: ignore[method-assign]
    lambda w, h: render_log.append(("frame", w, h))
)
render_engine.resize_render = lambda w, h: render_log.append(("size", w, h))  # type: ignore[method-assign]
gl_window.video.resize(320, 180)
gl_window.video.initializeGL()
gl_window.video.paintGL()
gl_window.video.resizeGL(640, 360)
check(bool(render_log) and render_log[0][0] == "attach",
      f"initializeGL did not call attach_render: {render_log}")
check(gl_window.video._render_attached is True,
      "successful attach_render did not mark the surface attached")
gl_window.video.initializeGL()
check(len([e for e in render_log if e[0] == "attach"]) == 1,
      "initializeGL re-attached an already attached surface")
check(render_log.count(("update",)) == 1,
      f"paintGL must run exactly one update: {render_log}")
# DPR is 2.0 on this surface: one render per paint with scaled size.
check([entry for entry in render_log if entry[0] == "frame"] == [("frame", 640, 360)],
      f"paintGL must render once at width*dpr, height*dpr: {render_log}")
check(("size", 640, 360) in render_log, "resizeGL did not notify the engine")

# Continuous render loop: idle engine must not run the driver, and a
# loaded source starts it after paint; one tick is one update-per-paint.
check(gl_window.video._repaint_timer.interval() == 33,
      "video repaint driver interval must be ~33 ms")
check(not gl_window.video._repaint_timer.isActive(),
      "repaint driver must stay idle without a source")
render_engine._transition(dp.PlayerState.LOADING)
before_tick = render_log.count(("update",))
gl_window.video.isVisible = lambda: True  # type: ignore[method-assign]
gl_window.video.paintGL()
check(gl_window.video._repaint_timer.isActive(),
      "paintGL did not start the continuous repaint driver")
check(render_log.count(("update",)) == before_tick + 1,
      "paint must still run exactly one update with the driver armed")
gl_window.video.stop_render_loop()
check(not gl_window.video._repaint_timer.isActive(),
      "stop_render_loop left the repaint driver running")

# Loki d: hidden surfaces never re-arm, and a close raised during a render
# cannot be undone by the paintGL that follows in the same frame.
gl_window.video.isVisible = lambda: False  # type: ignore[method-assign]
gl_window.video.paintGL()
check(not gl_window.video._repaint_timer.isActive(),
      "hidden surface re-armed the repaint driver")
gl_window.video.isVisible = lambda: True  # type: ignore[method-assign]
gl_window.video.paintGL()
check(gl_window.video._repaint_timer.isActive(),
      "visible surface failed to arm the repaint driver")
_render_before = render_engine.render_frame
render_engine.render_frame = (  # type: ignore[method-assign]
    lambda w, h: gl_window.video.closeEvent(QCloseEvent())
)
gl_window.video.paintGL()
check(not gl_window.video._repaint_timer.isActive(),
      "close mid-render restarted the repaint driver")
check(gl_window.video._closed is True, "closeEvent did not latch _closed")
render_engine.render_frame = _render_before  # type: ignore[method-assign]
gl_window.video.paintGL()
check(not gl_window.video._repaint_timer.isActive(),
      "paint after close re-armed the repaint driver")

# Regression: a refused attach_render falls back to attach_widget(winId()).
fallback_engine = dp.DvdPlaybackEngine()
fallback_engine.render_mode = "render"  # type: ignore[attr-defined]
fallback_attach: list[object] = []
fallback_wid: list[int] = []
fallback_engine.attach_render = (  # type: ignore[method-assign]
    lambda proc, fbo=None: (fallback_attach.append(proc), False)[1]
)
fallback_engine.attach_widget = lambda wid: fallback_wid.append(wid)  # type: ignore[method-assign]
fb_window = dp.MainWindow(fallback_engine)
fb_window._auto_detected = True
fb_widget = fb_window.video
check(isinstance(fb_widget, _winmod.VideoGLWidget), "fallback needs a GL surface")
expected_wid = int(fb_widget.winId())
fb_widget.initializeGL()
check(len(fallback_attach) == 1, "fallback path did not attempt attach_render")
check(fallback_wid == [expected_wid],
      f"failed attach_render did not fall back to winId(): {fallback_wid}")
check(fb_widget._render_attached is False,
      "failed attach_render must not mark the surface attached")
fb_window.close()

# Regression: when the wid fallback also fails, initializeGL stays quiet
# (the engine's software path owns the last resort) and never corrupts state.
broken_engine = dp.DvdPlaybackEngine()
broken_engine.render_mode = "render"  # type: ignore[attr-defined]
broken_engine.attach_render = lambda proc, fbo=None: False  # type: ignore[method-assign]


def _broken_wid(wid: int) -> None:
    raise dp.PlaybackError("wid unavailable")


broken_engine.attach_widget = _broken_wid  # type: ignore[method-assign]
broken_window = dp.MainWindow(broken_engine)
broken_window._auto_detected = True
broken_widget = broken_window.video
check(isinstance(broken_widget, _winmod.VideoGLWidget), "broken fallback needs GL")
broken_widget.initializeGL()
check(broken_widget._render_attached is False,
      "failed wid fallback marked the surface attached")
broken_window.close()

# FBO wiring: initializeGL hands the engine a callable that re-reads the
# widget's live defaultFramebufferObject; the recorder proves it is invoked
# on every paint and its current return value is what the frame uses.
fbo_engine = dp.DvdPlaybackEngine()
fbo_engine.render_mode = "render"  # type: ignore[attr-defined]
fbo_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
fbo_attach: list[object] = []
fbo_reads: list[int] = []


def _attach_with_fbo(proc: object, fbo: object = None) -> bool:
    fbo_attach.append(fbo)
    return True


def _render_with_fbo(width: int, height: int) -> bool:
    del width, height
    provider = fbo_attach[0]
    if callable(provider):
        fbo_reads.append(int(provider()))
    return True


fbo_engine.attach_render = _attach_with_fbo  # type: ignore[method-assign]
fbo_engine.update = lambda: False  # type: ignore[method-assign]
fbo_engine.render_frame = _render_with_fbo  # type: ignore[method-assign]
fbo_window = dp.MainWindow(fbo_engine)
fbo_window._auto_detected = True
fbo_widget = fbo_window.video
check(isinstance(fbo_widget, _winmod.VideoGLWidget), "FBO wiring needs GL")
fbo_ids = iter([101, 202])
fbo_widget.defaultFramebufferObject = lambda: next(fbo_ids)  # type: ignore[method-assign]
fbo_widget.initializeGL()
check(len(fbo_attach) == 1 and callable(fbo_attach[0]),
      f"initializeGL did not pass an FBO callable: {fbo_attach}")
fbo_widget.paintGL()
fbo_widget.paintGL()
check(fbo_reads == [101, 202],
      f"FBO provider was not re-read per paint: {fbo_reads}")
fbo_window.close()

# Engine-level forwarding: attach_render hands the same FBO source to the
# render context (positional third argument), not FBO 0.
_engine_mod = sys.modules["dvd_player.engine"]
_orig_render_context = _engine_mod.RenderContext
_orig_mpv_factory = _engine_mod.mpv.MPV


class _MiniRenderPlayer:
    def __init__(self, **options: object) -> None:
        self.options = options

    def property_observer(self, name: str):  # noqa: ANN201 - mpv decorator
        del name
        return lambda fn: fn

    def terminate(self) -> None:
        return None


class _RecordingRenderContext:
    seen: list[object] = []

    def __init__(self, player: object, proc: object, fbo: object = None) -> None:
        del player, proc
        self.fbo = fbo
        _RecordingRenderContext.seen.append(fbo)

    def update(self) -> bool:
        return True

    def render(self, width: int, height: int) -> None:
        del width, height

    def free(self) -> None:
        return None


try:
    _engine_mod.RenderContext = _RecordingRenderContext  # type: ignore[assignment]
    _engine_mod.mpv.MPV = _MiniRenderPlayer
    fwd_engine = dp.DvdPlaybackEngine()
    fbo_provider = lambda: 303  # noqa: E731 - tiny recorder
    check(
        fwd_engine.attach_render(lambda name: 0, fbo=fbo_provider) is True,
        "engine attach_render did not accept an FBO source",
    )
    check(_RecordingRenderContext.seen == [fbo_provider],
          f"engine did not forward the FBO source: {_RecordingRenderContext.seen}")
    fwd_engine.dispose()
finally:
    _engine_mod.RenderContext = _orig_render_context
    _engine_mod.mpv.MPV = _orig_mpv_factory

# Regression: QOpenGLContext.getProcAddress() only accepts bytes, while the
# render.py callback passes the GL name as str. The widget loader wrapper must
# re-encode, or every lookup raises TypeError and the render create fails.
import ctypes as _ctypes  # noqa: E402

_render_mod = sys.modules["dvd_player.render"]
_qt_proc_names: list[object] = []


def _fake_qt_get_proc_address(name: object) -> int:
    _qt_proc_names.append(name)
    if not isinstance(name, (bytes, bytearray)):
        raise TypeError(
            f"getProcAddress(...): argument 1 has unexpected type "
            f"'{type(name).__name__}'"
        )
    return 0x5EED


_wrapped_proc = _winmod._bytes_proc_loader(_fake_qt_get_proc_address)
check(_wrapped_proc("glGetString") == 0x5EED,
      "wrapped GL proc loader did not forward the symbol")
check(_qt_proc_names == [b"glGetString"],
      f"getProcAddress received {_qt_proc_names!r}, not bytes")

_orig_render_create = _render_mod.mpv._mpv_render_context_create
try:
    _render_mod.mpv._mpv_render_context_create = lambda buf, handle, params: 0
    _proc_ctx = _render_mod.RenderContext(
        type("_Handle", (), {"handle": "mpv"})(), _wrapped_proc
    )
    _init: dict[int, object] = {}
    _idx = 0
    while _proc_ctx.create_params[_idx].type_id != 0:
        _init[_proc_ctx.create_params[_idx].type_id] = _proc_ctx.create_params[_idx]
        _idx += 1
    _gl_init = _ctypes.cast(
        _init[2].data, _ctypes.POINTER(_render_mod.mpv.MpvOpenGLInitParams)
    ).contents
    check(_gl_init.get_proc_address(None, b"glGetString") == 0x5EED,
          "render-context create path failed on the GL proc loader")
    check(_qt_proc_names[-1] == b"glGetString",
          f"render callback leaked a str to getProcAddress: {_qt_proc_names!r}")
finally:
    _render_mod.mpv._mpv_render_context_create = _orig_render_create

# Regression: render/update exceptions never crash the paint loop or
# corrupt the attached state; the driver still runs once per paint.
render_engine._render_attached = True
state_counts: list[str] = []


def _boom_update() -> bool:
    state_counts.append("update")
    raise RuntimeError("update boom")


def _boom_render(width: int, height: int) -> bool:
    state_counts.append(f"render {width}x{height}")
    raise RuntimeError("render boom")


render_engine.update = _boom_update  # type: ignore[method-assign]
render_engine.render_frame = _boom_render  # type: ignore[method-assign]
gl_window.video.paintGL()
check(state_counts == ["update", "render 640x360"],
      f"faulty driver ran wrong calls: {state_counts}")
check(gl_window.video._render_attached is True,
      "render exception corrupted the attached state")
gl_window.video.paintGL()
check(state_counts == ["update", "render 640x360"] * 2,
      f"paint loop stopped after a fault: {state_counts}")

# R7: overlays stay siblings above the GL surface with full-window geometry.
gl_window.resize(1280, 720)
gl_window.resizeEvent(None)
check(gl_window.home.parentWidget() is gl_window,
      "home overlay is not a sibling above the video")
check(gl_window.chapter_grid.parentWidget() is gl_window,
      "chapter grid is not a sibling above the video")
check(gl_window.help.parentWidget() is gl_window,
      "help overlay is not a sibling above the video")
for _overlay in (gl_window.home, gl_window.chapter_grid, gl_window.help):
    check(_overlay.geometry().getRect() == (0, 0, 1280, 720),
          f"overlay geometry {_overlay.geometry().getRect()}")
check(gl_window.osd.geometry().getRect() == (0, 720 - 64, 1280, 64),
      "OSD geometry wrong over the GL surface")
gl_window.close()

# ── (q) 2026 live title preview: one load per blade-session ─────────
_prev_devices = dp.list_dvd_devices
_prev_present = dp.disc_present
dp.list_dvd_devices = lambda: ["/dev/sr0"]  # type: ignore[assignment]
dp.disc_present = lambda dev: True  # type: ignore[assignment]

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
check(pv_loads == ["/dev/sr0"], f"preview did not boot once: {pv_loads}")
check(pv_window._preview_running, "preview flag not set")
check(pv_window.home.preview_live, "dashboard did not advertise live preview")

# Blade away and back within the same session never reloads.
pv_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right.value,
              Qt.KeyboardModifier.NoModifier)
)
pv_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left.value,
              Qt.KeyboardModifier.NoModifier)
)
check(pv_loads == ["/dev/sr0"], f"blade re-highlight reloaded: {pv_loads}")
pv_window._open_home()
check(pv_loads == ["/dev/sr0"], f"reopening the dashboard reloaded: {pv_loads}")

# ENTER reveals the already-running menu without a second load.
pv_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return.value,
              Qt.KeyboardModifier.NoModifier)
)
check(pv_loads == ["/dev/sr0"], f"ENTER reloaded the disc: {pv_loads}")
check(pv_window.home.isHidden(), "ENTER did not reveal the live menu")
check(pv_window._preview_revealed, "reveal state not recorded")

# Esc returns to the dashboard; the menu keeps running behind it.
pv_window.keyPressEvent(
    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape.value,
              Qt.KeyboardModifier.NoModifier)
)
check(not pv_window.home.isHidden(), "Esc did not return to the dashboard")
check(not pv_window._preview_revealed, "Esc left the reveal state set")
check(pv_loads == ["/dev/sr0"], f"Esc reloaded the disc: {pv_loads}")

# Eject tears the preview down.
pv_window._on_eject_clicked()
check(not pv_window._preview_running, "eject left preview running")
check(not pv_window.home.preview_live, "eject left the live badge on")

# App close also tears down an active preview.
pv_window._disc_presence = {"/dev/sr0": True}
pv_window._open_home()
check(pv_loads == ["/dev/sr0", "/dev/sr0"], "preview did not restart")
pv_window.close()
check(not pv_window._preview_running, "close left preview running")

# Resume prompt is deferred from preview boot to the ENTER reveal.
_orig_resume_get = dp.ResumeStore.get
_orig_resume_forget = dp.ResumeStore.forget


def _make_preview_window() -> tuple[dp.MainWindow, dp.DvdPlaybackEngine]:
    engine = dp.DvdPlaybackEngine()
    engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
    engine.load = lambda src: None  # type: ignore[method-assign]
    window = dp.MainWindow(engine)
    window._auto_detected = True
    window._attach_engine = lambda: None  # type: ignore[method-assign]
    window._disc_presence = {"/dev/sr0": True}
    window._open_home()
    return window, engine


def _press_enter(window: dp.MainWindow) -> None:
    window.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return.value,
                  Qt.KeyboardModifier.NoModifier)
    )


# (a) hash during preview: zero dialogs, the entry is stashed.
rs_window, rs_engine = _make_preview_window()
_resume_calls: list[dict] = []  # type: ignore[type-arg]
rs_engine.resume_disc = lambda saved: _resume_calls.append(saved)  # type: ignore[method-assign]
dp.ResumeStore.get = lambda self, h: {"pos": 65.0}  # type: ignore[assignment]
_dialogs["question"].clear()
rs_window._on_disc_hash("preview-hash")
check(_dialogs["question"] == [], "resume dialog fired during the quiet preview")
check(rs_window._preview_saved == {"pos": 65.0}, "resume state was not deferred")
check(rs_window._preview_hash == "preview-hash", "preview hash not stashed")

# (b) ENTER selects the disc: exactly one prompt, Yes resumes.
_question_answer = QMessageBox.StandardButton.Yes
_press_enter(rs_window)
check(len(_dialogs["question"]) == 1, "ENTER did not show the deferred prompt once")
check(_resume_calls == [{"pos": 65.0}], f"Yes did not resume: {_resume_calls}")
rs_window.close()

# (b2) No forgets the saved entry and never resumes.
no_window, no_engine = _make_preview_window()
_no_resume: list[dict] = []  # type: ignore[type-arg]
_forgets: list[str] = []
no_engine.resume_disc = lambda saved: _no_resume.append(saved)  # type: ignore[method-assign]
dp.ResumeStore.forget = lambda self, h: _forgets.append(h)  # type: ignore[method-assign]
dp.ResumeStore.get = lambda self, h: {"pos": 30.0}  # type: ignore[assignment]
_dialogs["question"].clear()
no_window._on_disc_hash("preview-no")
check(_dialogs["question"] == [], "No-path dialog fired during the preview")
_question_answer = QMessageBox.StandardButton.No
_press_enter(no_window)
check(len(_dialogs["question"]) == 1, "ENTER did not show exactly one No prompt")
check(_forgets == ["preview-no"], f"No did not forget the entry: {_forgets}")
check(_no_resume == [], "No branch resumed")
no_window.close()

# (c) ENTER with no saved entry only reveals (no dialog).
none_window, none_engine = _make_preview_window()
dp.ResumeStore.get = lambda self, h: None  # type: ignore[assignment]
_dialogs["question"].clear()
none_window._on_disc_hash("preview-none")
check(none_window._preview_saved is None, "empty hash stashed a resume entry")
_press_enter(none_window)
check(_dialogs["question"] == [], "ENTER without an entry opened a dialog")
check(none_window.home.isHidden(), "ENTER without an entry did not reveal")
none_window.close()

# (d) a newer no-entry hash invalidates the previous preview stash.
stale_window, stale_engine = _make_preview_window()
_stale_resumes: list[dict] = []  # type: ignore[type-arg]
_stale_forgets: list[str] = []
stale_engine.resume_disc = lambda saved: _stale_resumes.append(saved)  # type: ignore[method-assign]
dp.ResumeStore.get = (  # type: ignore[assignment]
    lambda self, h: {"pos": 65.0} if h == "disc-a" else None
)
dp.ResumeStore.forget = lambda self, h: _stale_forgets.append(h)  # type: ignore[method-assign]
stale_window._on_disc_hash("disc-a")
check(stale_window._preview_saved == {"pos": 65.0}, "first hash not stashed")
stale_window._on_disc_hash("disc-b")
check(stale_window._preview_saved is None, "no-entry hash kept the stale stash")
check(stale_window._preview_hash is None, "no-entry hash kept the stale hash")
_dialogs["question"].clear()
_press_enter(stale_window)
check(_dialogs["question"] == [], "stale stash prompted for the previous disc")
check(_stale_resumes == [], "stale stash resumed the previous disc")
check(_stale_forgets == [], "stale stash forgot the previous disc")
stale_window.close()
dp.ResumeStore.get = _orig_resume_get  # type: ignore[assignment]
dp.ResumeStore.forget = _orig_resume_forget  # type: ignore[assignment]

# Veil transparency: the video stays visible through the frosted glass,
# yet it is tinted (not a raw passthrough) and the redesign tokens hold.
veil_menu = dp.HomeMenu()
veil_menu.resize(1280, 720)
check(veil_menu.PANEL_RADIUS == 16.0, "panel radius is not 16 px")
check(veil_menu.ROW_RADIUS >= 12.0, "row radius lost its modern scale")
check(120 <= veil_menu.VEIL_ALPHA <= 210, "veil alpha outside the legible band")
check(len(veil_menu._blade_rects()) == 3, "blade structure changed")
check(veil_menu.footer_text() ==
      "Enter select  ·  ←/→ blades  ·  Esc back  ·  Ctrl+H help",
      "footer hints drifted")
veil_canvas = QImage(1280, 720, QImage.Format.Format_ARGB32)
veil_canvas.fill(QColor(210, 24, 24))
veil_menu.render(veil_canvas)
_veil_px = veil_canvas.pixelColor(1275, 715)
check(_veil_px.red() > 40, f"video invisible through veil: {_veil_px.red()}")
check(_veil_px.red() < 210, f"veil did not tint the video: {_veil_px.red()}")
veil_menu.deleteLater()

dp.list_dvd_devices = _prev_devices  # type: ignore[assignment]
dp.disc_present = _prev_present  # type: ignore[assignment]

# ── (r) attach ordering: loads wait for attach_complete ────────────
gate_engine = dp.DvdPlaybackEngine()
gate_engine.render_mode = "render"  # type: ignore[attr-defined]
gate_loads: list[str] = []
gate_engine.attach_render = lambda proc, fbo=None: True  # type: ignore[method-assign]
gate_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
gate_engine.load = lambda src: gate_loads.append(src)  # type: ignore[method-assign]
gate_window = dp.MainWindow(gate_engine)
gate_window._auto_detected = True
gate_window._attach_engine = lambda: None  # type: ignore[method-assign]
gate_window._start_preview("/dev/sr0")
check(gate_loads == [],
      f"preview loaded before the attach-complete signal: {gate_loads}")
gate_window.video.initializeGL()
check(gate_loads == ["/dev/sr0"],
      f"queued preview did not flush after attach: {gate_loads}")
gate_window.close()

fail_engine = dp.DvdPlaybackEngine()
fail_engine.render_mode = "render"  # type: ignore[attr-defined]
fail_loads: list[str] = []
fail_engine.load = lambda src: fail_loads.append(src)  # type: ignore[method-assign]
fail_engine.attach_render = lambda proc, fbo=None: False  # type: ignore[method-assign]


def _refuse_wid(wid: int) -> None:
    raise dp.PlaybackError(
        "engine not attached to a video widget yet (attach_widget first)"
    )


fail_engine.attach_widget = _refuse_wid  # type: ignore[method-assign]
fail_window = dp.MainWindow(fail_engine)
fail_window._auto_detected = True
fail_window._attach_engine = lambda: None  # type: ignore[method-assign]
_dialogs["critical"].clear()
fail_window._play_source("/dev/sr0")
check(fail_loads == [], "unattached load reached engine.load before attach")
fail_window.video.initializeGL()
check(fail_loads == [], "failed attach still loaded the queued source")
check("renderer unavailable" in fail_window.status.text(),
      f"attach failure did not show the graceful status: {fail_window.status.text()!r}")
check(_dialogs["critical"] == [], "attach failure surfaced a PlaybackError dialog")
fail_window._play_source("/dev/sr0")
check(fail_loads == [], "load after a failed attach still reached engine.load")
check(_dialogs["critical"] == [], "post-failure load surfaced a raw error dialog")
fail_window.close()

# Loki regression: a queued load must not survive eject or close and be
# flushed by a late attach_complete.
def _queued_gl_window() -> tuple[dp.MainWindow, list[str]]:
    engine = dp.DvdPlaybackEngine()
    engine.render_mode = "render"  # type: ignore[attr-defined]
    loads: list[str] = []
    engine.attach_render = lambda proc, fbo=None: True  # type: ignore[method-assign]
    engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
    engine.load = lambda src: loads.append(src)  # type: ignore[method-assign]
    engine.eject = lambda: None  # type: ignore[method-assign]
    window = dp.MainWindow(engine)
    window._auto_detected = True
    window._attach_engine = lambda: None  # type: ignore[method-assign]
    window._play_source("/dev/sr0")
    check(window._pending_load is not None, "queued load was not recorded")
    return window, loads


eject_window, eject_loads = _queued_gl_window()
eject_window._on_eject_clicked()
eject_window.video.initializeGL()
check(eject_loads == [], f"late attach flushed a load after eject: {eject_loads}")
eject_window.close()

close_window, close_loads = _queued_gl_window()
close_window.close()
close_window.video.initializeGL()
check(close_loads == [], f"late attach flushed a load after close: {close_loads}")

# ── (s) startup: dashboard paints before any device probe + debug log ──
import tempfile as _startup_tmp
from pathlib import Path as _StartupPath

import dvd_player.app as _dvd_app

_log_dir = _startup_tmp.mkdtemp(prefix="dvd-player-log-")
_log_path = _StartupPath(_log_dir) / "dvd-player.log"
check(_dvd_app.configure_debug_log(_log_path) == _log_path,
      "configure_debug_log returned the wrong path")
check(_log_path.is_file(), "startup debug log was not created")

# Other windows from earlier sections own disc timers; stop them so the
# ordering recorder below only sees this window's startup probe.
for _widget in app.topLevelWidgets():
    if isinstance(_widget, dp.MainWindow) and _widget is not None:
        _timer = getattr(_widget, "_disc_timer", None)
        if _timer is not None:
            _timer.stop()

_startup_order: list[str] = []
_orig_startup_devices = dp.list_dvd_devices
_orig_startup_present = dp.disc_present


def _probe_recorder(device: str) -> bool:
    check(startup_window.home._painted_once,
          "device probe ran before the dashboard's first paint")
    check(not startup_window.home.isHidden(),
          "device probe ran while the dashboard was hidden")
    _startup_order.append(device)
    return True


def _devices_recorder() -> list[str]:
    check(startup_window.home._painted_once,
          "device enumeration ran before the dashboard's first paint")
    return ["/dev/sr0"]


startup_engine = dp.DvdPlaybackEngine()
startup_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
startup_window = dp.MainWindow(startup_engine)
startup_window.resize(1280, 720)
dp.list_dvd_devices = _devices_recorder  # type: ignore[assignment]
dp.disc_present = _probe_recorder  # type: ignore[assignment]
startup_window.show()
_deadline = time.monotonic() + 3.0
while time.monotonic() < _deadline:
    app.processEvents()
    if startup_window._startup_done and startup_window.home._painted_once:
        break
    time.sleep(0.01)
app.processEvents()
check(startup_window.home._painted_once, "dashboard never painted")
check(not startup_window.home.isHidden(),
      "dashboard was not visible on the first frame")
check(startup_window._startup_done, "deferred startup never ran")
check(_startup_order == ["/dev/sr0"],
      f"startup probe order drifted: {_startup_order}")

_log_text = _log_path.read_text(encoding="utf-8")
for _needle in (
    "app start",
    "window shown",
    "first paint",
    "home menu shown",
    "device probe",
    "attach attempt",
    "attach result",
):
    check(_needle in _log_text, f"debug log missing {_needle!r}: {_log_text!r}")
check(len(_log_text.splitlines()) <= _dvd_app.DEBUG_LOG_MAX_LINES + 1,
      "debug log is not capped")

# Loki regression: a hostile device name must not forge a second line.
_hostile_device = (
    "/dev/sr0\n2026-01-01T00:00:00.000 INFO dvd_player: "
    "milestone app start: forged"
)
startup_window._probe_device(_hostile_device, milestone=True)
_hostile_log = _log_path.read_text(encoding="utf-8")
check("\\n" in _hostile_log, "hostile device name was not repr-escaped")
_forged = [
    line for line in _hostile_log.splitlines()
    if line.endswith("milestone app start: forged")
]
check(_forged == [], f"hostile device name forged a log line: {_forged}")

dp.list_dvd_devices = _orig_startup_devices  # type: ignore[assignment]
dp.disc_present = _orig_startup_present  # type: ignore[assignment]
startup_window.close()

# ── (z2) render dispatch matrix: DVD_PLAYER_RENDER -> attach path ────
# Env naming and the platform gate must agree: auto/render on Wayland
# reaches attach_render(proc, fbo) and never touches attach_widget; xcb
# keeps the wid path; software on Wayland attempts render then degrades.
check(_winmod.RENDER_MODE_ENV == "DVD_PLAYER_RENDER",
      f"render env name drifted: {_winmod.RENDER_MODE_ENV}")
for _value in ("auto", "render"):
    check(_winmod.render_mode_requested({"DVD_PLAYER_RENDER": _value}),
          f"DVD_PLAYER_RENDER={_value} did not select render mode")
check(not _winmod.render_mode_requested({"DVD_PLAYER_RENDER": "software"}),
      "DVD_PLAYER_RENDER=software wrongly selected render mode")
check(_winmod.render_mode_requested({"DVD_PLAYER_RENDER_MODE": "render"}),
      "legacy render-mode spelling stopped working")

_orig_wid_safe = _winmod.wid_embedding_safe
_orig_platform = _winmod.VideoGLWidget._platform_name
_orig_render_env = os.environ.pop("DVD_PLAYER_RENDER", None)
_orig_software = os.environ.get("LIBGL_ALWAYS_SOFTWARE")
_dispatch_windows: list[dp.MainWindow] = []
try:
    # (1) DVD_PLAYER_RENDER=auto on native Wayland -> attach_render.
    _winmod.wid_embedding_safe = lambda: False
    _winmod.VideoGLWidget._platform_name = staticmethod(lambda: "wayland")
    os.environ["DVD_PLAYER_RENDER"] = "auto"
    auto_engine = dp.DvdPlaybackEngine()
    auto_render: list[tuple[object, object]] = []
    auto_wid: list[int] = []
    auto_engine.attach_render = (  # type: ignore[method-assign]
        lambda proc, fbo=None: (auto_render.append((proc, fbo)), True)[1]
    )
    auto_engine.attach_widget = lambda wid: auto_wid.append(wid)  # type: ignore[method-assign]
    auto_window = dp.MainWindow(auto_engine)
    auto_window._auto_detected = True
    _dispatch_windows.append(auto_window)
    check(isinstance(auto_window.video, _winmod.VideoGLWidget),
          "DVD_PLAYER_RENDER=auto on Wayland did not build the GL surface")
    auto_widget = auto_window.video
    auto_widget.initializeGL()
    check(len(auto_render) == 1,
          f"auto/Wayland did not call attach_render once: {auto_render}")
    check(callable(auto_render[0][1]),
          f"attach_render did not receive an FBO callable: {auto_render[0]}")
    check(auto_wid == [], f"auto/Wayland wrongly used attach_widget: {auto_wid}")
    check(auto_widget._render_attached is True,
          "auto/Wayland attach_render success was not recorded")

    # (2) software on xcb -> wid path, never attach_render.
    _winmod.wid_embedding_safe = lambda: True
    _winmod.VideoGLWidget._platform_name = staticmethod(lambda: "xcb")
    os.environ["DVD_PLAYER_RENDER"] = "software"
    xcb_engine = dp.DvdPlaybackEngine()
    xcb_render: list[object] = []
    xcb_wid: list[int] = []
    xcb_engine.attach_render = (  # type: ignore[method-assign]
        lambda proc, fbo=None: (xcb_render.append(proc), True)[1]
    )
    xcb_engine.attach_widget = lambda wid: xcb_wid.append(wid)  # type: ignore[method-assign]
    xcb_window = dp.MainWindow(xcb_engine)
    xcb_window._auto_detected = True
    _dispatch_windows.append(xcb_window)
    check(not isinstance(xcb_window.video, _winmod.VideoGLWidget),
          "software on xcb mistakenly built the GL surface")
    xcb_window._attach_engine()
    check(xcb_wid == [int(xcb_window.video.winId())],
          f"software/xcb did not take the wid path: {xcb_wid}")
    check(xcb_render == [], f"software/xcb wrongly called attach_render: {xcb_render}")

    # (3) software on Wayland -> one render attempt, no wid fallback,
    #     then the graceful status (wid embedding is impossible).
    _winmod.wid_embedding_safe = lambda: False
    _winmod.VideoGLWidget._platform_name = staticmethod(lambda: "wayland")
    os.environ["DVD_PLAYER_RENDER"] = "software"
    os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"  # skip the one-shot retry
    soft_engine = dp.DvdPlaybackEngine()
    soft_render: list[object] = []
    soft_wid: list[int] = []
    soft_engine.attach_render = (  # type: ignore[method-assign]
        lambda proc, fbo=None: (soft_render.append(proc), False)[1]
    )
    soft_engine.attach_widget = lambda wid: soft_wid.append(wid)  # type: ignore[method-assign]
    soft_window = dp.MainWindow(soft_engine)
    soft_window._auto_detected = True
    _dispatch_windows.append(soft_window)
    check(isinstance(soft_window.video, _winmod.VideoGLWidget),
          "software on Wayland did not attempt the GL surface")
    soft_window.video.initializeGL()
    check(len(soft_render) == 1,
          f"software/Wayland did not attempt attach_render: {soft_render}")
    check(soft_wid == [], f"software/Wayland wrongly fell back to wid: {soft_wid}")
    check(soft_window._attach_failed is True,
          "software/Wayland failure was not recorded")
    check("renderer unavailable" in soft_window.status.text(),
          f"software/Wayland missed the graceful status: {soft_window.status.text()!r}")
finally:
    for _window in _dispatch_windows:
        _window.close()
    _winmod.wid_embedding_safe = _orig_wid_safe  # type: ignore[method-assign]
    _winmod.VideoGLWidget._platform_name = staticmethod(_orig_platform)  # type: ignore[method-assign]
    if _orig_render_env is None:
        os.environ.pop("DVD_PLAYER_RENDER", None)
    else:
        os.environ["DVD_PLAYER_RENDER"] = _orig_render_env
    if _orig_software is None:
        os.environ.pop("LIBGL_ALWAYS_SOFTWARE", None)
    else:
        os.environ["LIBGL_ALWAYS_SOFTWARE"] = _orig_software

# ── (z3) 2026 boot splash: startup, loading state, no orphan timers ──
splash_engine = dp.DvdPlaybackEngine()
splash_engine.attach_widget = lambda wid: None  # type: ignore[method-assign]
splash_window = dp.MainWindow(splash_engine)
splash_window._attach_engine = lambda: None  # type: ignore[method-assign]
splash_window.resize(960, 540)
check(isinstance(splash_window.splash, _winmod.BootSplash),
      "MainWindow did not build a BootSplash")
check(splash_window.splash.isHidden() and not splash_window.splash.is_animating(),
      "splash is visible/animating before startup")
splash_window.show()
# showEvent shows the dashboard then the splash: the splash must be on
# screen BEFORE the first home paint.
check(not splash_window.home._painted_once,
      "home painted before the boot splash could appear")
check(splash_window.splash.isVisible(), "boot splash was not shown at startup")
check(splash_window.splash.is_animating(),
      "boot splash shimmer is not ticking while visible")
splash_image = splash_window.splash.grab().toImage()
check(not splash_image.isNull(), "boot splash paint produced no image")
_green_px = 0
for _y in range(0, splash_image.height(), 4):
    for _x in range(0, splash_image.width(), 4):
        _px = splash_image.pixelColor(_x, _y)
        if (
            abs(_px.red() - 116) <= 35
            and abs(_px.green() - 195) <= 35
            and abs(_px.blue() - 41) <= 35
        ):
            _green_px += 1
check(_green_px > 20, f"boot splash has no Xbox-green ring: {_green_px} px")
_splash_deadline = time.monotonic() + 3.0
while time.monotonic() < _splash_deadline and not splash_window.home._painted_once:
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
check(splash_window.home._painted_once, "home menu never painted")
check(not splash_window.splash.isVisible(),
      "boot splash survived the home menu's first paint")
check(not splash_window.splash.is_animating(),
      "boot splash left its shimmer timer running after hide")

# Disc load: 'Loading disc…' (+ label when known) until playback advances.
splash_window._show_loading_splash("/dev/sr0")
check(splash_window.splash.isVisible(), "loading splash did not appear")
check(splash_window.splash.status_text() == "Loading disc…",
      f"wrong loading headline: {splash_window.splash.status_text()!r}")
check(splash_window.splash.disc_label() is None,
      "a raw block device leaked as a disc label")
splash_window.splash.show_loading("TEST DISC")
check(splash_window.splash.disc_label() == "TEST DISC",
      "known disc label was not shown on the loading splash")
_ticks_before = splash_window.splash._phase
_splash_tick_deadline = time.monotonic() + 0.25
while time.monotonic() < _splash_tick_deadline:
    app.processEvents()
    time.sleep(0.01)
app.processEvents()
check(splash_window.splash._phase != _ticks_before,
      "shimmer did not advance: the event loop was blocked")
splash_window._on_engine_state(dp.PlayerState.LOADING)
check(splash_window.splash.isVisible(),
      "loading splash hid while the disc was still LOADING")
splash_window._on_engine_state(dp.PlayerState.PLAYING)
check(not splash_window.splash.isVisible(),
      "loading splash stayed over the video after playback started")
check(not splash_window.splash.is_animating(),
      "loading splash left its shimmer timer running")
check(not splash_window._splash_timer.isActive(),
      "loading splash left its safety timer running")
splash_window.close()

print(f"GUI SMOKE OK {_count} assertions")
