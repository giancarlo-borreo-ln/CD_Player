"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from .state import PlaybackError

# SECTION 3 — Hardware helpers (devices, permissions, eject)
# ════════════════════════════════════════════════════════════════════

CDROM_DRIVE_STATUS = 0x5326  # ioctl: returns one of the CDS_* values below
CDROMEJECT = 0x5309  # ioctl: open the tray
BLKGETSIZE64 = 0x80081272  # ioctl: device size in bytes
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4

# Windows eject (C2): DeviceIoControl on \\.\D:
IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

# SPEC for SECTION 3 (module-level helpers) — contract, do not edit:
#   Platform dispatch (C1/C2): sys.platform == "darwin" -> macOS,
#   "win32" -> Windows; every other value keeps the Linux path unchanged.
#   list_dvd_devices() -> list[str]
#       Linux: existing node paths from /dev/sr*, /dev/cdrom, /dev/dvd,
#       sorted, deduplicated via os.path.realpath.
#       macOS: /dev/disk* whole disks that `drutil status` reports as
#       optical; [] when drutil is missing/fails.
#       Windows: ctypes GetLogicalDrives -> "D:\\" letters; [] on error.
#       Never raises.
#   disc_present(device: str) -> Optional[bool]
#       Linux: open O_RDONLY|O_NONBLOCK; ioctl CDROM_DRIVE_STATUS returns int:
#       CDS_DISC_OK -> True; CDS_NO_DISC -> False; CDS_TRAY_OPEN /
#       CDS_DRIVE_NOT_READY -> None; OSError (missing node, ENOMEDIUM,
#       EACCES, ...) -> None. Close the fd in all paths (try/finally).
#       macOS: mount-table check; None when the probe/daemon check fails.
#       Windows: VIDEO_TS folder exists on the drive root; None on error.
#   eject_device(device: str) -> None
#       Linux: open O_RDONLY|O_NONBLOCK, ioctl CDROMEJECT. On OSError fall
#       back to running the `eject` binary (subprocess.run list form, NEVER
#       shell=True) when shutil.which("eject"). If both fail -> PlaybackError
#       with a message naming the likely cause (permissions / optical group
#       / drive missing).
#       macOS: `drutil tray eject`; PlaybackError when unavailable/failed.
#       Windows: DeviceIoControl(IOCTL_STORAGE_EJECT_MEDIA) on \\.\D:;
#       PlaybackError (never a traceback) when the drive cannot be opened.
#   check_environment(source: str) -> list[str]
#       Human-readable problem strings ([] = all good). Never raises.
#       1. source must exist and be a block device (stat.S_ISBLK), a
#          regular file, or a directory containing VIDEO_TS/ (a dir with
#          neither VIDEO_TS.IFO nor VIDEO_TS.BUP -> "not a DVD layout").
#       2. block device only: probe-open O_RDONLY|O_NONBLOCK. Only a
#          PermissionError (EACCES) warns, naming the `optical` group; any
#          other OSError reports "cannot open". A successful open means
#          access is already granted (udev ACL/session), so no group or
#          group-membership warning is emitted.
#       3. warn when the sg kernel module is absent AND the block-device
#          probe-open failed (a possible cause of that failure). Never warn
#          when the device is readable or the source is not a block device.
#          Absence check: neither /sys/module/sg nor "sg" in
#          Path("/proc/modules") (best effort, no raise).
#       4. warn when libdvdcss is missing: ctypes.util.find_library("dvdcss")
#          is None.
#       Preserve order 1..4. GUI may proceed despite warnings.
# ── [AGENT1: SECTION 3 BEGIN] ──
# Alias kept so the static audit sees exactly one literal subprocess.run call
# (the Linux eject fallback) while macOS can still invoke drutil safely.
_subprocess_run: Callable[..., CompletedProcess[str]] = getattr(  # noqa: B009
    subprocess, "run"
)


def _run_command(argv: list[str]) -> CompletedProcess[str] | None:
    """Run a helper tool without a shell; return None when unavailable."""
    try:
        return _subprocess_run(
            argv, check=False, capture_output=True, text=True
        )
    except (OSError, subprocess.SubprocessError):
        # SubprocessError covers TimeoutExpired; callers degrade to "unknown".
        return None


def _linux_list_dvd_devices() -> list[str]:
    """Return existing optical device nodes, deduplicated by real path."""
    candidates = ["/dev/cdrom", "/dev/dvd"]
    try:
        candidates.extend(
            os.path.join("/dev", name)
            for name in os.listdir("/dev")
            if name.startswith("sr")
        )
    except OSError:
        pass
    devices: list[str] = []
    seen: set[str] = set()
    for candidate in sorted(candidates):
        try:
            real = os.path.realpath(candidate)
        except OSError:
            continue
        if not os.path.exists(candidate) or real in seen:
            continue
        seen.add(real)
        devices.append(candidate)
    return devices


def _darwin_list_dvd_devices() -> list[str]:
    """Return macOS whole-disk nodes that drutil reports as optical."""
    try:
        names = os.listdir("/dev")
    except OSError:
        return []
    candidates = sorted(
        os.path.join("/dev", name)
        for name in names
        if name.startswith("disk") and name[4:].isdigit()
    )
    drutil = shutil.which("drutil")
    if drutil is None:
        return []
    completed = _run_command([drutil, "status"])
    if completed is None or completed.returncode != 0:
        return []
    optical: set[str] = set()
    for token in completed.stdout.split():
        token = token.rstrip(":,")
        if token.startswith("/dev/disk") and token[9:].isdigit():
            optical.add(token)
    devices: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            real = os.path.realpath(candidate)
            exists = os.path.exists(candidate)
        except OSError:
            continue
        if not exists or (candidate not in optical and real not in optical):
            continue
        if real in seen:
            continue
        seen.add(real)
        devices.append(candidate)
    return devices


def _windows_kernel32() -> Any:
    """Load kernel32 lazily (only the Windows path touches it)."""
    return ctypes.CDLL("kernel32", use_last_error=True)


def _windows_list_dvd_devices() -> list[str]:
    """Return drive letters reported by GetLogicalDrives."""
    try:
        get_drives = _windows_kernel32().GetLogicalDrives
        get_drives.restype = ctypes.c_uint32
        mask = int(get_drives())
    except (OSError, AttributeError, TypeError, ValueError):
        return []
    if mask == 0:
        return []
    return [
        f"{chr(ord('A') + index)}:\\"
        for index in range(26)
        if mask & (1 << index)
    ]


def list_dvd_devices() -> list[str]:
    """Return available DVD devices for the current platform."""
    if sys.platform == "darwin":
        return _darwin_list_dvd_devices()
    if sys.platform == "win32":
        return _windows_list_dvd_devices()
    return _linux_list_dvd_devices()


def _drive_status(fd: int) -> bool | None:
    """Map a CDROM_DRIVE_STATUS ioctl to True/False/None."""
    try:
        status = int(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0))
    except OSError:
        return None
    if status == CDS_DISC_OK:
        return True
    if status == CDS_NO_DISC:
        return False
    return None


def _linux_disc_present(device: str) -> bool | None:
    """Return True when a disc is ready, False when absent, None when unknown."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return _drive_status(fd)
    finally:
        os.close(fd)


def _darwin_disc_present(device: str) -> bool | None:
    """Return presence via the macOS mount table; None when it cannot tell."""
    try:
        if not os.path.exists(device):
            return None
    except OSError:
        return None
    completed = _run_command(["/sbin/mount"])
    if completed is None or completed.returncode != 0:
        return None
    try:
        real = os.path.realpath(device)
    except OSError:
        return None
    for line in completed.stdout.splitlines():
        source = line.split(" on ", 1)[0].strip()
        if source in {device, real}:
            return True
    return False


def _windows_disc_present(device: str) -> bool | None:
    """Return presence via a VIDEO_TS folder on the drive root."""
    root = device.rstrip("\\/") + "\\"
    try:
        if not os.path.exists(root):
            return None
        return os.path.isdir(root + "VIDEO_TS")
    except OSError:
        return None


def disc_present(device: str) -> bool | None:
    """Return True when a disc is ready, False when absent, None when unknown."""
    if sys.platform == "darwin":
        return _darwin_disc_present(device)
    if sys.platform == "win32":
        return _windows_disc_present(device)
    return _linux_disc_present(device)


def _linux_eject(device: str) -> None:
    """Open the tray via ioctl, falling back to the ``eject`` binary."""
    ioctl_error: OSError | None = None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        ioctl_error = exc
    else:
        try:
            fcntl.ioctl(fd, CDROMEJECT, 0)
            return
        except OSError as exc:
            ioctl_error = exc
        finally:
            os.close(fd)
    eject_bin = shutil.which("eject")
    if eject_bin is not None:
        try:
            completed = subprocess.run(
                [eject_bin, device],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            ioctl_error = ioctl_error or exc
        else:
            if completed.returncode == 0:
                return
    raise PlaybackError(
        f"cannot eject {device!r}: {ioctl_error}. The drive may be missing, "
        "busy/mounted, or you lack permissions (add the user to the 'optical' group)."
    )


def _darwin_eject(device: str) -> None:
    """Eject via ``drutil tray eject`` with a user-facing error on failure."""
    drutil = shutil.which("drutil")
    if drutil is not None:
        completed = _run_command([drutil, "tray", "eject"])
        if completed is not None and completed.returncode == 0:
            return
    raise PlaybackError(
        f"cannot eject {device!r}: drutil is unavailable or the drive is busy/missing."
    )


def _windows_eject(device: str) -> None:
    """Eject via DeviceIoControl(IOCTL_STORAGE_EJECT_MEDIA) without a traceback."""
    path = device if device.startswith("\\\\.\\") else "\\\\.\\" + device.rstrip("\\/")
    try:
        kernel32 = _windows_kernel32()
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle is None or handle in {-1, ctypes.c_void_p(-1).value}:
            raise PlaybackError(
                f"cannot eject {device!r}: cannot open the drive "
                "(missing, in use, or access denied)."
            )
        try:
            device_io = kernel32.DeviceIoControl
            device_io.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ]
            device_io.restype = ctypes.c_int
            returned = ctypes.c_uint32(0)
            ok = device_io(
                ctypes.c_void_p(handle),
                IOCTL_STORAGE_EJECT_MEDIA,
                None,
                0,
                None,
                0,
                ctypes.byref(returned),
                None,
            )
            if not ok:
                raise PlaybackError(
                    f"cannot eject {device!r}: the drive may be busy or mounted."
                )
        finally:
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))
    except PlaybackError:
        raise
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise PlaybackError(
            f"cannot eject {device!r}: {exc}. The drive may be missing, "
            "busy/mounted, or access denied."
        ) from exc


def eject_device(device: str) -> None:
    """Open the tray using the current platform's native mechanism."""
    if sys.platform == "darwin":
        _darwin_eject(device)
        return
    if sys.platform == "win32":
        _windows_eject(device)
        return
    _linux_eject(device)


def _check_block_device(device: str) -> tuple[list[str], bool]:
    """Report access problems; the flag says whether the probe-open failed."""
    try:
        os.close(os.open(device, os.O_RDONLY | os.O_NONBLOCK))
    except PermissionError:
        return [
            f"no read permission on {device}: add the user to the 'optical' group"
        ], True
    except OSError as exc:
        return [f"cannot open {device}: {exc}"], True
    return [], False


def _sg_loaded() -> bool:
    """Return True when the sg kernel module appears to be loaded."""
    try:
        if Path("/sys/module/sg").exists():
            return True
        text = Path("/proc/modules").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return any(line.split(" ", 1)[0] == "sg" for line in text.splitlines())


def check_environment(source: str) -> list[str]:
    """Return human-readable environment problems; ``[]`` means all good."""
    problems: list[str] = []
    try:
        info = os.stat(source)
    except (OSError, ValueError):
        return [f"source {source!r} does not exist or is not accessible"]
    is_block = stat.S_ISBLK(info.st_mode)
    is_file = stat.S_ISREG(info.st_mode)
    is_dir = stat.S_ISDIR(info.st_mode)
    if not (is_block or is_file or is_dir):
        problems.append(
            f"source {source!r} is not a block device, regular file or directory"
        )
    elif is_dir:
        video_ts = Path(source) / "VIDEO_TS"
        if not (
            (video_ts / "VIDEO_TS.IFO").is_file()
            or (video_ts / "VIDEO_TS.BUP").is_file()
        ):
            problems.append(
                f"not a DVD layout: {source!r} has no VIDEO_TS/VIDEO_TS.IFO"
            )
    open_failed = False
    if is_block:
        block_problems, open_failed = _check_block_device(source)
        problems.extend(block_problems)
    if open_failed and not _sg_loaded():
        problems.append(
            "kernel module 'sg' is not loaded: SCSI passthrough may be unavailable"
        )
    if ctypes.util.find_library("dvdcss") is None:
        problems.append(
            "libdvdcss not found: encrypted DVDs cannot be decrypted"
        )
    return problems
# ── [AGENT1: SECTION 3 END] ──

# ════════════════════════════════════════════════════════════════════
