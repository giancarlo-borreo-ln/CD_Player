"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

from typing import Any, ClassVar

from PyQt6.QtCore import QObject, QTimer, pyqtClassInfo, pyqtProperty, pyqtSlot
from PyQt6.QtDBus import QDBusAbstractAdaptor, QDBusConnection

from .engine import DvdPlaybackEngine
from .state import PlayerState, log


class _MprisRootAdaptor(QDBusAbstractAdaptor):
    """org.mpris.MediaPlayer2 root interface."""

    def __init__(self, service: MprisService) -> None:
        super().__init__(service)
        self._service = service

    @pyqtSlot()
    def Raise(self) -> None:
        """Ask the host window to come forward."""

    @pyqtSlot()
    def Quit(self) -> None:
        """MPRIS Quit: not implemented (the app owns its lifetime)."""

    @pyqtProperty(str)
    def Identity(self) -> str:
        """Human-readable application identity."""
        return "DVD Player"

    @pyqtProperty(bool)
    def CanQuit(self) -> bool:
        """Whether Quit is supported."""
        return False

    @pyqtProperty(bool)
    def CanRaise(self) -> bool:
        """Whether Raise is supported."""
        return True

    @pyqtProperty(bool)
    def HasTrackList(self) -> bool:
        """No MPRIS track list is exported."""
        return False


@pyqtClassInfo(  # type: ignore[func-returns-value]
    "D-Bus Interface", "org.mpris.MediaPlayer2.Player"
)
class _MprisPlayerAdaptor(QDBusAbstractAdaptor):  # type: ignore[misc]
    """org.mpris.MediaPlayer2.Player interface."""

    def __init__(self, service: MprisService) -> None:
        super().__init__(service)
        self._service = service

    @pyqtSlot()
    def PlayPause(self) -> None:
        self._service.PlayPause()

    @pyqtSlot()
    def Play(self) -> None:
        self._service.Play()

    @pyqtSlot()
    def Pause(self) -> None:
        self._service.Pause()

    @pyqtSlot()
    def Stop(self) -> None:
        self._service.Stop()

    @pyqtSlot()
    def Next(self) -> None:
        self._service.Next()

    @pyqtSlot()
    def Previous(self) -> None:
        self._service.Previous()

    @pyqtSlot(int)
    def Seek(self, offset: int) -> None:
        self._service.Seek(offset)

    @pyqtSlot(object, int)
    def SetPosition(self, track_id: object, position: int) -> None:
        del track_id
        self._service.SetPosition(position)

    @pyqtSlot(float)
    def SetVolume(self, volume: float) -> None:
        self._service.SetVolume(volume)

    @pyqtProperty(str)
    def PlaybackStatus(self) -> str:
        return self._service.PlaybackStatus()

    @pyqtProperty(int)
    def Position(self) -> int:
        return int(self._service.Position() * 1_000_000.0)

    @pyqtProperty(float)
    def Volume(self) -> float:
        return self._service.Volume()

    @pyqtProperty(dict)
    def Metadata(self) -> dict[str, object]:
        return self._service.Metadata()

    @pyqtProperty(bool)
    def CanControl(self) -> bool:
        return True

    @pyqtProperty(bool)
    def CanPlay(self) -> bool:
        return True

    @pyqtProperty(bool)
    def CanPause(self) -> bool:
        return True

    @pyqtProperty(bool)
    def CanGoNext(self) -> bool:
        return True

    @pyqtProperty(bool)
    def CanGoPrevious(self) -> bool:
        return True

    @pyqtProperty(bool)
    def CanSeek(self) -> bool:
        return True


class MprisService(QObject):
    """MPRIS2 media-key bridge; bus acquisition is injectable for tests."""

    SERVICE_NAME: ClassVar[str] = "org.mpris.MediaPlayer2.dvd_player"
    OBJECT_PATH: ClassVar[str] = "/org/mpris/MediaPlayer2"

    def __init__(
        self,
        engine: DvdPlaybackEngine,
        bus: Any | None = None,
        parent: QObject | None = None,
    ) -> None:
        """Create the adaptors and track the engine position."""
        super().__init__(parent)  # type: ignore[arg-type]
        self.engine = engine
        self._bus: Any | None = bus
        self._registered = False
        self._position = 0.0
        self._root = _MprisRootAdaptor(self)
        self._player = _MprisPlayerAdaptor(self)
        engine.position_changed.connect(self._on_position)
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(1000)
        self._position_timer.timeout.connect(self._refresh_position)
        self._position_timer.start()

    # ── registration (non-fatal on duplicate/unavailable bus) ───────
    def start(self) -> bool:
        """Register the service name and object; False when unavailable."""
        bus = self._bus
        if bus is None:
            try:
                bus = QDBusConnection.sessionBus()
            except Exception:  # noqa: BLE001 - no session bus is not fatal
                bus = None
            self._bus = bus
        if bus is None:
            return False
        try:
            if not bool(bus.isConnected()):
                return False
            bus.registerService(self.SERVICE_NAME)  # False on duplicate: OK
            bus.registerObject(
                self.OBJECT_PATH,
                self,
                QDBusConnection.RegisterOption.ExportAllSlots,
            )
        except Exception as exc:  # noqa: BLE001 - media keys are optional
            log.warning("MPRIS registration unavailable: %s", exc)
            return False
        self._registered = True
        return True

    def stop(self) -> None:
        """Release the exported object and service name (idempotent)."""
        bus = self._bus
        if bus is not None:
            try:
                bus.unregisterObject(self.OBJECT_PATH)
            except Exception:  # noqa: BLE001, S110 - teardown is best-effort
                pass
            try:
                bus.unregisterService(self.SERVICE_NAME)
            except Exception:  # noqa: BLE001, S110 - teardown is best-effort
                pass
        self._registered = False

    @property
    def registered(self) -> bool:
        """Whether start() succeeded for this instance."""
        return self._registered

    # ── position tracking ───────────────────────────────────────────
    def _on_position(self, seconds: float) -> None:
        try:
            self._position = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return

    def _refresh_position(self) -> None:
        value = getattr(self.engine, "time_pos", None)
        if isinstance(value, (int, float)):
            self._position = max(0.0, float(value))

    # ── MPRIS Player mapping ────────────────────────────────────────
    def PlaybackStatus(self) -> str:
        """Map the engine state to the MPRIS vocabulary."""
        state = getattr(self.engine, "state", None)
        if state is PlayerState.PLAYING:
            return "Playing"
        if state is PlayerState.PAUSED:
            return "Paused"
        return "Stopped"

    def Position(self) -> float:
        """Current position in seconds."""
        self._refresh_position()
        return self._position

    def Volume(self) -> float:
        """Current engine volume (0 when unknown)."""
        value = getattr(self.engine, "volume", None)
        return float(value) if isinstance(value, (int, float)) else 0.0

    def Metadata(self) -> dict[str, object]:
        """MPRIS metadata with the disc label as xesam:title."""
        label = getattr(self.engine, "disc_label", None)
        title = label if isinstance(label, str) and label else "DVD"
        return {"xesam:title": title, "xesam:artist": ["DVD"]}

    def PlayPause(self) -> None:
        self.engine.play_pause()

    def Play(self) -> None:
        if getattr(self.engine, "state", None) is PlayerState.PAUSED:
            self.engine.play_pause()

    def Pause(self) -> None:
        if getattr(self.engine, "state", None) is PlayerState.PLAYING:
            self.engine.play_pause()

    def Stop(self) -> None:
        self.engine.stop()

    def Next(self) -> None:
        self.engine.next_chapter()

    def Previous(self) -> None:
        self.engine.previous_chapter()

    def Seek(self, offset: int) -> None:
        """Seek by a signed microsecond offset from the current position."""
        base = getattr(self.engine, "time_pos", None)
        start = float(base) if isinstance(base, (int, float)) else 0.0
        try:
            delta = float(offset) / 1_000_000.0
        except (TypeError, ValueError):
            return
        self.engine.seek_to(max(0.0, start + delta))

    def SetPosition(self, position: int) -> None:
        """Seek to an absolute MPRIS position (microseconds)."""
        try:
            seconds = float(position) / 1_000_000.0
        except (TypeError, ValueError):
            return
        self.engine.seek_to(max(0.0, seconds))

    def SetVolume(self, volume: float) -> None:
        """Set the absolute engine volume through adjust_volume."""
        current = self.Volume()
        try:
            target = max(0.0, min(150.0, float(volume)))
        except (TypeError, ValueError):
            return
        self.engine.adjust_volume(target - current)


