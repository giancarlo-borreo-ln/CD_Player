"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import os
import shutil
import stat
import subprocess
from pathlib import Path

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

# SPEC for SECTION 3 (module-level helpers) — contract, do not edit:
#   list_dvd_devices() -> list[str]
#       Existing node paths from /dev/sr*, /dev/cdrom, /dev/dvd, sorted,
#       deduplicated via os.path.realpath. Never raises.
#   disc_present(device: str) -> Optional[bool]
#       Open O_RDONLY|O_NONBLOCK; ioctl CDROM_DRIVE_STATUS returns int:
#       CDS_DISC_OK -> True; CDS_NO_DISC -> False; CDS_TRAY_OPEN /
#       CDS_DRIVE_NOT_READY -> None; OSError (missing node, ENOMEDIUM,
#       EACCES, ...) -> None. Close the fd in all paths (try/finally).
#   eject_device(device: str) -> None
#       Open O_RDONLY|O_NONBLOCK, ioctl CDROMEJECT. On OSError fall back to
#       running the `eject` binary (subprocess.run list form, NEVER
#       shell=True) when shutil.which("eject"). If both fail -> PlaybackError
#       with a message naming the likely cause (permissions / optical group
#       / drive missing).
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
def list_dvd_devices() -> list[str]:
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


def disc_present(device: str) -> bool | None:
    """Return True when a disc is ready, False when absent, None when unknown."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return _drive_status(fd)
    finally:
        os.close(fd)


def eject_device(device: str) -> None:
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
