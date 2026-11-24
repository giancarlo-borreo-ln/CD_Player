"""UAT driver — TEST EQUIPMENT ONLY (not part of the app).

Boots the real app offscreen, injects user-level inputs (mouse clicks, key
presses, typed text) and records OBSERVABLE evidence: window state, status
label text, dialogs (recorded instead of blocking), OSD visibility, pixel
metrics, clean-exit code. It prints a plain-text transcript for the blind
acceptance agent. It does NOT judge the user experience — the agent does.
"""
import locale
import os
import random
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path  # noqa: E402

from PyQt6.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

import dvd_player as dp  # noqa: E402
import mpv as mpv_mod  # noqa: E402

app = QApplication([])
# Qt resets LC_NUMERIC at app init; libmpv requires the C locale (segfaults
# with comma-decimal locales) — re-assert AFTER the app exists.
locale.setlocale(locale.LC_NUMERIC, "C")

# Equipment calibration: the offscreen QPA has no native window handles, so
# real wid-embedding (the desktop path) cannot run here; swap the wid kwarg
# for a headless null VO while keeping the engine's own attach code and
# property observers intact. Also shorten the load watchdog so unreadable
# discs are observable within the transcript window.
dp.LOAD_TIMEOUT_MS = 4000


class HeadlessMPV(mpv_mod.MPV):
    def __init__(self, *a, **kw):
        kw.pop("wid", None)
        kw["vo"] = "null"
        kw["ao"] = "null"
        super().__init__(*a, **kw)


dp.mpv.MPV = HeadlessMPV

# Equipment: record dialogs instead of blocking the run.
dialogs: list[tuple[str, str]] = []
QMessageBox.warning = staticmethod(
    lambda *a, **k: dialogs.append(("warning", str(a[2]) if len(a) > 2 else ""))
    or QMessageBox.StandardButton.Ok
)
QMessageBox.critical = staticmethod(
    lambda *a, **k: dialogs.append(("critical", str(a[2]) if len(a) > 2 else ""))
    or QMessageBox.StandardButton.Ok
)
QMessageBox.question = staticmethod(
    lambda *a, **k: dialogs.append(("question", str(a[2]) if len(a) > 2 else ""))
    or QMessageBox.StandardButton.Yes
)


def wait(ms: int) -> None:
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


print("=== S1: launch ===")
engine = dp.DvdPlaybackEngine()
window = dp.MainWindow(engine)
window.resize(1280, 720)
window.show()
wait(300)
print("window visible:", window.isVisible())
print("status:", repr(window.status.text()))
print("source placeholder:", repr(window.source.placeholderText()))
print("engine state:", engine.state.name)

print("=== S2: garbage source -> friendly feedback, no crash ===")
buttons = {b.text(): b for b in window.findChildren(QPushButton)}
print("buttons:", sorted(buttons))
dialogs.clear()
window.source.setText("$(reboot) /etc/passwd")
QTest.mouseClick(buttons["Load / Play"], Qt.MouseButton.LeftButton)
wait(300)
print("dialogs:", dialogs)
print("alive:", True)

print("=== S3: key reveals OSD; OSD auto-hides ===")
window.osd.hide()
QTest.keyClick(window, Qt.Key.Key_Right)
wait(100)
print("osd visible after key:", window.osd.isVisible())
wait(4600)
print("osd visible after 4.6s:", window.osd.isVisible())

print("=== S4: OSD transport buttons ===")
clicks: list[str] = []
window.osd.button_clicked.connect(clicks.append)
window.osd.show_osd()
wait(80)
rects = window.osd._button_rects()
print("osd button count:", len(rects))
for name, rect in rects:
    QTest.mouseClick(window.osd, Qt.MouseButton.LeftButton,
                     pos=QPoint(int(rect.center().x()), int(rect.center().y())))
    wait(20)
print("button clicks:", clicks)

print("=== S5: load a disc (fake VIDEO_TS) + resume prompt ===")
# Eject the real disc first so the engine accepts a new load, and route the
# app's ResumeStore to a temp file seeded with a known entry.
QTest.mouseClick(buttons["Eject"], Qt.MouseButton.LeftButton)
wait(300)
tmp = tempfile.mkdtemp(prefix="uat_disc_")
disc = Path(tmp)
(disc / "VIDEO_TS").mkdir()
(disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"FAKE" * 200)
store = dp.ResumeStore(Path(tmp) / "resume.json")
store.save(dp.DiscIdentity.compute(str(disc)), 65.0, 1, 3)
dp.ResumeStore = lambda *a, **k: store
dialogs.clear()
window.source.setText(str(disc))
QTest.mouseClick(buttons["Load / Play"], Qt.MouseButton.LeftButton)
wait(1500)
print("dialogs:", dialogs)
wait(4500)
print("status after load:", repr(window.status.text()))
print("engine state:", engine.state.name)
saw_playing = engine.state is dp.PlayerState.PLAYING
assert saw_playing, f"disc did not reach PLAYING (state={engine.state.name})"
assert not any(
    "multiple values" in str(d).lower() or "mpv" in str(d).lower() for d in dialogs
), f"mpv init error surfaced in dialogs: {dialogs}"

print("=== S6: eject ===")
dialogs.clear()
QTest.mouseClick(buttons["Eject"], Qt.MouseButton.LeftButton)
wait(300)
print("status:", repr(window.status.text()))
print("engine state:", engine.state.name)

print("=== S7: key mash + resize abuse ===")
random.seed(7)
for _ in range(300):
    QTest.keyClick(window, random.choice([
        Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right,
        Qt.Key.Key_Return, Qt.Key.Key_Space, Qt.Key.Key_F, Qt.Key.Key_R,
        Qt.Key.Key_I, Qt.Key.Key_O, Qt.Key.Key_Escape, Qt.Key.Key_E]))
    wait(5)
for size in ((0, 0), (320, 240), (1280, 720), (1920, 1080)):
    window.resize(*size)
    wait(50)
print("alive after mash+resize:", True)
print("final status:", repr(window.status.text()))

print("=== S8: pixel evidence (bar rendering + hover green) ===")
window.resize(1280, 720)
# Simulate the black video surface the OSD normally sits on.
from PyQt6.QtGui import QColor, QPalette
pal = window.osd.palette()
pal.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0))
window.osd.setPalette(pal)
window.osd.setAutoFillBackground(True)
window.osd.set_transport(state=dp.PlayerState.PLAYING, speed=1.0,
                         time_pos=65.0, duration=5400.0, chapter=3,
                         chapter_count=12, audio_lang="en", subtitle_lang="fr")
window.osd.show_osd()
wait(100)
img = window.osd.grab().toImage()
dark = total = 0
for x in range(0, img.width(), 4):
    for y in range(0, img.height(), 2):
        c = img.pixelColor(x, y)
        total += 1
        if c.red() < 60 and c.green() < 60 and c.blue() < 60 and c.alpha() > 100:
            dark += 1
print("osd size:", img.width(), "x", img.height())
print("dark-bar pixel ratio:", round(dark / max(1, total), 3))
rect = dict((n, r) for n, r in window.osd._button_rects())["audio"]
# QTest.mouseMove does not dispatch hover moves reliably to child widgets
# on the offscreen QPA; inject the move event directly (equipment).
from PyQt6.QtCore import QEvent as _QEvent, QPointF  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
window.osd.mouseMoveEvent(QMouseEvent(
    _QEvent.Type.MouseMove, QPointF(rect.center()),
    Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
    Qt.KeyboardModifier.NoModifier))
wait(100)
img2 = window.osd.grab().toImage()
green = 0
for x in range(0, img2.width(), 3):
    for y in range(0, img2.height(), 3):
        c = img2.pixelColor(x, y)
        if c.green() > 120 and c.green() > c.red() * 1.5 and c.green() > c.blue() * 2:
            green += 1
print("hover-green pixels:", green)
img.save("/tmp/uat_osd.png")
img2.save("/tmp/uat_osd_hover.png")

print("=== S9: clean close (subprocess) ===")
code = (
    "import os, locale\nos.environ['QT_QPA_PLATFORM']='offscreen'\n"
    "from PyQt6.QtWidgets import QApplication\n"
    "from PyQt6.QtCore import QTimer\n"
    "import dvd_player as dp\nimport mpv as mpv_mod\n"
    "app=QApplication([])\nlocale.setlocale(locale.LC_NUMERIC,'C')\n"
    "dp.list_dvd_devices=lambda:[]\n"
    "class H(mpv_mod.MPV):\n"
    "    def __init__(self,*a,**kw):\n"
    "        kw.pop('wid',None)\n"
    "        kw['vo']='null'; kw['ao']='null'\n"
    "        super().__init__(*a,**kw)\n"
    "dp.mpv.MPV=H\n"
    "e=dp.DvdPlaybackEngine()\n"
    "w=dp.MainWindow(e)\n"
    "w.show()\nQTimer.singleShot(200,w.close)\nQTimer.singleShot(600,app.quit)\n"
    "app.exec()\nprint('CLOSE CLEAN')\n"
)
run = subprocess.run([sys.executable, "-c", code], capture_output=True,
                     text=True, timeout=60)
print("close exit code:", run.returncode)
print("close output tail:", (run.stdout or run.stderr).strip().splitlines()[-1:])

# Leave the tray closed (the Eject scenarios opened it).
try:
    import fcntl
    fd = os.open("/dev/cdrom", os.O_RDONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, 0x5319)  # CDROMCLOSETRAY
    os.close(fd)
    print("tray closed")
except OSError:
    print("tray close skipped")
print("UAT DRIVER DONE")
assert saw_playing, "PLAYING was never observed"
print("DRIVER-OK")
