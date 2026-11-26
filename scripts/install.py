#!/usr/bin/env python3
"""Cross-platform installer/checker for DVD Player.

Standard library only. Common checks run first (Python, uv, project env,
imports, native libraries); then one explicit platform branch handles the
OS-specific setup: Linux, macOS or Windows.

    python3 scripts/install.py          # check + `uv sync`
    python3 scripts/install.py --no-sync
    python3 scripts/install.py --disc /dev/sr0   # validate one disc source

Exit codes: 0 = ready to run, 1 = a hard dependency is missing or --disc is
invalid, 2 = usage/wrong-platform error.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 12)

# Set from --platform so the launchers and the Windows .exe can force a branch
# even when the interpreter reports a different sys.platform (e.g. MSYS).
_OS_OVERRIDE: str | None = None


def current_os() -> str:
    """Map sys.platform to a branch key: linux, macos, windows or other."""
    if _OS_OVERRIDE:
        return _OS_OVERRIDE
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in ("win32", "cygwin", "msys"):
        return "windows"
    return "other"


def distro_family() -> str:
    """Return 'arch', 'debian' or '' for Linux hints."""
    if current_os() != "linux":
        return ""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    fields = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value.strip().strip('"')
    ids = (fields.get("ID", "") + " " + fields.get("ID_LIKE", "")).lower()
    if "arch" in ids:
        return "arch"
    if "debian" in ids or "ubuntu" in ids:
        return "debian"
    return ""


def hint_python() -> str:
    key = current_os()
    if key == "windows":
        return "winget install --id Python.Python.3.12"
    if key == "macos":
        return "brew install python@3.12"
    return "install Python 3.12+ with your package manager"


def hint_uv() -> str:
    key = current_os()
    if key == "windows":
        return "winget install --id astral-sh.uv  (or: pip install uv)"
    if key == "macos":
        return "brew install uv"
    return "sudo pacman -S uv  (or: pip install --user uv)"


def hint_libmpv() -> str:
    key = current_os()
    distro = distro_family()
    if key == "windows":
        return (
            "libmpv is not shipped with mpv for Windows: put libmpv-2.dll in "
            "%LOCALAPPDATA%\\dvd-player\\lib or the venv's Scripts directory "
            "(or on PATH); install mpv.net for a system-wide libmpv."
        )
    if key == "macos":
        return "brew install mpv  (Homebrew installs libmpv.dylib)"
    if distro == "arch":
        return "sudo pacman -S mpv"
    if distro == "debian":
        return "sudo apt install libmpv2 libmpv-dev"
    return "install libmpv with your distribution's package manager"


def hint_libdvdcss() -> str:
    key = current_os()
    distro = distro_family()
    if key == "windows":
        return (
            "experimental: CSS decryption needs libdvdcss-2.dll from the VideoLAN "
            "project in %LOCALAPPDATA%\\dvd-player\\lib or the venv's Scripts "
            "directory (or on PATH); unencrypted discs work without it."
        )
    if key == "macos":
        return "brew install libdvdcss"
    if distro == "arch":
        return "sudo pacman -S libdvdcss"
    if distro == "debian":
        return "sudo apt install libdvdcss2"
    return "install libdvdcss with your distribution's package manager"


def hint_libdvdnav() -> str:
    key = current_os()
    distro = distro_family()
    if key == "windows":
        return (
            "dvdnav.dll must sit next to libmpv in %LOCALAPPDATA%\\dvd-player\\lib "
            "or the venv's Scripts directory; mpv builds usually bundle it."
        )
    if key == "macos":
        return "brew install libdvdnav"
    if distro == "arch":
        return "sudo pacman -S libdvdnav"
    if distro == "debian":
        return "sudo apt install libdvdnav4"
    return "install libdvdnav with your distribution's package manager"


def native_names() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Library names to probe for mpv, dvdcss and dvdnav."""
    key = current_os()
    if key == "windows":
        return (
            ("mpv-2.dll", "mpv-1.dll"),
            ("dvdcss.dll", "libdvdcss-2.dll"),
            ("dvdnav.dll", "libdvdnav-4.dll", "libdvdnav.dll"),
        )
    if key == "macos":
        return (
            ("mpv", "libmpv.dylib"),
            ("dvdcss", "libdvdcss.dylib"),
            ("dvdnav", "libdvdnav.dylib"),
        )
    return (
        ("mpv", "libmpv.so.2"),
        ("dvdcss", "libdvdcss.so.2"),
        ("dvdnav", "libdvdnav.so.4"),
    )


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, title: str, detail: str = "") -> None:
        print(f"  [ok]   {title}" + (f" — {detail}" if detail else ""))

    def warn(self, title: str, detail: str) -> None:
        self.warnings += 1
        print(f"  [warn] {title}")
        print(f"         {detail}")

    def fail(self, title: str, detail: str) -> None:
        self.failures += 1
        print(f"  [fail] {title}")
        print(f"         {detail}")

    def section(self, title: str) -> None:
        print(f"\n== {title} ==")


def venv_python() -> Path | None:
    if current_os() == "windows":
        candidate = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = REPO_ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else None


def check_import(python: Path, module: str) -> tuple[bool, str]:
    result = subprocess.run(
        [str(python), "-c", f"import {module}"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode == 0:
        return True, ""
    lines = (result.stderr or result.stdout).strip().splitlines()
    return False, lines[-1] if lines else f"import {module} failed"


def check_python(report: Report) -> bool:
    report.section("Python")
    if sys.version_info < MIN_PYTHON:
        report.fail(
            f"Python {'.'.join(map(str, MIN_PYTHON))}+ required",
            f"found {platform.python_version()} ({sys.executable}). {hint_python()}",
        )
        return False
    report.ok("Python", f"{platform.python_version()} ({sys.executable})")
    return True


def check_uv(report: Report) -> bool:
    report.section("uv")
    uv = shutil.which("uv")
    if not uv:
        report.fail("uv not found on PATH", f"install it: {hint_uv()}")
        return False
    try:
        version = subprocess.run(
            [uv, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        version = "unknown version"
    report.ok("uv", version or "present")
    return True


def check_sync(report: Report, run_sync: bool) -> None:
    report.section("Project environment")
    if not run_sync:
        report.ok("uv sync", "skipped (--no-sync)")
        return
    try:
        result = subprocess.run(["uv", "sync"], check=False, cwd=REPO_ROOT)
    except OSError as error:
        report.fail(
            "uv sync could not start",
            f"could not execute uv ({error}). Reinstall uv: {hint_uv()}",
        )
        return
    if result.returncode == 0:
        report.ok("uv sync", "environment is up to date")
    else:
        report.fail(
            "uv sync failed",
            "fix the error above, then re-run: python3 scripts/install.py",
        )


def check_imports(report: Report) -> None:
    report.section("Python packages")
    python = venv_python()
    if python is None:
        report.fail(
            "project venv not found",
            "run `uv sync` in the repository root to create .venv",
        )
        return
    for module, label in (("PyQt6.QtWidgets", "PyQt6"), ("mpv", "python-mpv")):
        ok, error = check_import(python, module)
        if ok:
            report.ok(label, f"import {module}")
        else:
            report.fail(f"{label} import failed", f"{error}\n         Try: uv sync")


def windows_search_dirs() -> list[Path]:
    """Directories worth scanning for Windows DLLs, in priority order."""
    candidates = [
        Path(sys.executable).parent,
        Path(sys.prefix) / "Scripts",
        Path(sys.base_prefix) / "Scripts",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata))
        candidates.append(Path(local_appdata) / "dvd-player" / "lib")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            candidates.append(Path(entry))
    seen: set[str] = set()
    unique: list[Path] = []
    for directory in candidates:
        key = str(directory).lower()
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def find_windows_dll(names: tuple[str, ...]) -> str | None:
    """find_library first, then scan known DLL directories for the given files."""
    for name in names:
        found = ctypes.util.find_library(name)
        if found:
            return found
    wanted = {name.lower() for name in names}
    for directory in windows_search_dirs():
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.name.lower() in wanted:
                    return str(entry)
            except OSError:
                continue
    return None


def find_any(names: tuple[str, ...]) -> str | None:
    if current_os() == "windows":
        return find_windows_dll(names)
    for name in names:
        found = ctypes.util.find_library(name)
        if found:
            return found
    return None


def check_native(report: Report, names: tuple[str, ...], label: str, hint: str, hard: bool) -> None:
    found = find_any(names)
    if found:
        report.ok(label, found)
    elif hard:
        report.fail(f"{label} not found", hint)
    else:
        report.warn(f"{label} not found (optional)", hint)


def platform_linux(report: Report) -> None:
    report.section("Linux setup")
    distro = distro_family()
    if distro == "arch":
        report.ok("distribution", "Arch Linux")
        print("         Packages: sudo pacman -S --needed mpv libdvdnav libdvdread libdvdcss")
    elif distro == "debian":
        report.ok("distribution", "Debian/Ubuntu")
        print("         Packages: sudo apt install mpv libmpv2 libdvdnav4 libdvdread8 libdvdcss2")
    else:
        report.ok("distribution", "unknown (using generic hints)")

    try:
        import grp

        try:
            gid = grp.getgrnam("optical").gr_gid
        except KeyError:
            gid = None
        if gid is not None and gid in os.getgroups():
            report.ok("optical group", "user is a member")
        else:
            report.warn(
                "user is not in the 'optical' group",
                "sudo usermod -aG optical $USER  (then log out and back in)",
            )
    except ImportError:
        report.warn("group check unavailable", "cannot import the grp module")

    sg_loaded = Path("/sys/module/sg").exists()
    if not sg_loaded:
        try:
            modules = Path("/proc/modules").read_text(encoding="utf-8", errors="replace")
            sg_loaded = any(line.split(" ", 1)[0] == "sg" for line in modules.splitlines())
        except OSError:
            pass
    if sg_loaded:
        report.ok("sg kernel module", "loaded")
    else:
        report.warn(
            "sg kernel module is not loaded",
            "sudo modprobe sg  (persist: echo sg | sudo tee /etc/modules-load.d/sg.conf)",
        )

    desktop = REPO_ROOT / "packaging" / "dvd-player.desktop"
    if desktop.exists():
        report.ok(
            "desktop entry",
            "install with: cp packaging/dvd-player.desktop ~/.local/share/applications/",
        )
    else:
        report.warn("desktop entry not found", f"expected at {desktop}")

    print("  Support: FULL — physical DVDs, ISO and VIDEO_TS folders.")


def platform_macos(report: Report) -> None:
    report.section("macOS setup")
    if shutil.which("brew"):
        report.ok("package manager", "Homebrew detected")
    else:
        report.warn(
            "Homebrew not found",
            "install it from https://brew.sh, then: brew install mpv libdvdnav libdvdcss",
        )
    print("         Install: brew install mpv libdvdnav libdvdcss")
    report.warn(
        "Homebrew library directory may not be on the loader path",
        "if libmpv/libdvdcss are not found, export "
        "DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib:$DYLD_FALLBACK_LIBRARY_PATH "
        "(Intel Macs: /usr/local/lib).",
    )
    print("  Support: EXPERIMENTAL — ISO and VIDEO_TS folder playback only;")
    print("           optical drive/disc handling is Linux-specific.")


def platform_windows(report: Report) -> None:
    report.section("Windows setup")
    report.warn(
        "Python and uv must be installed system-wide",
        f"{hint_python()}; {hint_uv()}",
    )
    report.warn(
        "libmpv load path",
        "ctypes searches PATH, not just the folder holding python.exe: put "
        "libmpv-2.dll, dvdnav.dll and libdvdcss-2.dll in "
        "%LOCALAPPDATA%\\dvd-player\\lib or the venv's Scripts directory "
        "(or on PATH). mpv.net provides a system-wide libmpv.",
    )
    print("  Support: EXPERIMENTAL — ISO and VIDEO_TS folder playback only;")
    print("           optical drive/disc handling is Linux-specific.")


def platform_other(report: Report) -> None:
    report.section("Unsupported platform")
    print(f"  Detected: {sys.platform} ({platform.system() or 'unknown'})")
    report.warn(
        "no platform-specific setup available",
        "the player is supported on Linux (full) and experimentally on macOS/Windows.",
    )
    print("  Support: UNKNOWN — generic hints only.")


PLATFORMS = {
    "linux": platform_linux,
    "macos": platform_macos,
    "windows": platform_windows,
    "other": platform_other,
}

# Accepted --platform values (aliases map to a branch key).
PLATFORM_ALIASES = {
    "auto": None,
    "linux": "linux",
    "macos": "macos",
    "darwin": "macos",
    "windows": "windows",
    "win32": "windows",
}


def normalize_platform(value: str) -> str | None:
    """Translate a --platform value to a branch key, or None for auto-detect."""
    return PLATFORM_ALIASES[value]


# ── Disc locations ──────────────────────────────────────────────────────────
# Mirrors the app's hardware layer without importing it (this script runs
# before the venv exists). Every probe degrades to "unknown" instead of raising.

CDS_NO_DISC = 1
CDS_DISC_OK = 4
CDROM_DRIVE_STATUS = 0x5326
DRIVE_CDROM = 5
MAX_SOURCES = 10
MAX_DEVICES = 10
PROBE_TIMEOUT = 5
DRUTIL_TIMEOUT = 3
SOURCE_DIRS = ("", "Videos", "Downloads", "Desktop")
SOURCE_MARKERS = ("VIDEO_TS.IFO", "VIDEO_TS.BUP")


def _run_tool(
    argv: list[str], timeout: float = PROBE_TIMEOUT
) -> subprocess.CompletedProcess[str] | None:
    """Run a helper with a hard timeout; None (unknown) on any failure."""
    try:
        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _linux_optical_devices() -> list[str] | None:
    candidates = ["/dev/cdrom", "/dev/dvd"]
    try:
        candidates.extend(
            os.path.join("/dev", name) for name in os.listdir("/dev") if name.startswith("sr")
        )
    except OSError:
        pass
    devices: list[str] = []
    seen: set[str] = set()
    for candidate in sorted(candidates):
        try:
            real = os.path.realpath(candidate)
            present = os.path.exists(candidate)
        except OSError:
            continue
        if present and real not in seen:
            seen.add(real)
            devices.append(candidate)
    return devices


def _darwin_optical_devices() -> list[str] | None:
    """Optical whole disks per drutil; None when drutil cannot tell."""
    drutil = shutil.which("drutil")
    if drutil is None:
        return None
    completed = _run_tool([drutil, "status"], timeout=DRUTIL_TIMEOUT)
    if completed is None or completed.returncode != 0:
        return None
    optical: set[str] = set()
    for token in completed.stdout.split():
        token = token.rstrip(":,")
        if token.startswith("/dev/disk") and token[9:].isdigit():
            optical.add(token)
    try:
        names = os.listdir("/dev")
    except OSError:
        return None
    devices: list[str] = []
    seen: set[str] = set()
    for name in sorted(names):
        if not (name.startswith("disk") and name[4:].isdigit()):
            continue
        candidate = os.path.join("/dev", name)
        try:
            real = os.path.realpath(candidate)
        except OSError:
            continue
        if (candidate in optical or real in optical) and real not in seen:
            seen.add(real)
            devices.append(candidate)
    return devices


def _windows_optical_devices() -> list[str] | None:
    """CD-ROM letters from GetLogicalDrives + GetDriveTypeW; None on error."""
    try:
        kernel32 = ctypes.CDLL("kernel32", use_last_error=True)
        get_drives = kernel32.GetLogicalDrives
        get_drives.restype = ctypes.c_uint32
        get_type = kernel32.GetDriveTypeW
        get_type.argtypes = [ctypes.c_wchar_p]
        get_type.restype = ctypes.c_uint32
        mask = int(get_drives())
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    if mask == 0:
        return []
    devices: list[str] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        try:
            if int(get_type(root)) == DRIVE_CDROM:
                devices.append(root)
        except (OSError, TypeError, ValueError):
            continue
    return devices


def optical_devices() -> list[str] | None:
    """Optical candidates for the branch; None means the probe is unavailable."""
    key = current_os()
    if key == "macos":
        return _darwin_optical_devices()
    if key == "windows":
        return _windows_optical_devices()
    if key == "linux":
        return _linux_optical_devices()
    return None


def _linux_state(device: str) -> bool | None:
    try:
        import fcntl
    except ImportError:
        return None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        try:
            status = int(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0))
        except OSError:
            return None
        if status == CDS_DISC_OK:
            return True
        if status == CDS_NO_DISC:
            return False
        return None
    finally:
        os.close(fd)


def _darwin_state(device: str) -> bool | None:
    completed = _run_tool(["/sbin/mount"])
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


def _windows_state(device: str) -> bool | None:
    root = device.rstrip("\\/") + "\\"
    try:
        if not os.path.exists(root):
            return None
        return os.path.isdir(root + "VIDEO_TS")
    except OSError:
        return None


def disc_state(device: str) -> bool | None:
    """True = disc ready, False = empty, None = unknown (never raises)."""
    key = current_os()
    if key == "macos":
        return _darwin_state(device)
    if key == "windows":
        return _windows_state(device)
    if key == "linux":
        return _linux_state(device)
    return None


def _has_ifo(video_ts: Path) -> bool:
    for marker in SOURCE_MARKERS:
        try:
            if (video_ts / marker).is_file():
                return True
        except OSError:
            continue
    return False


def classify_source(raw: str) -> tuple[str, str]:
    """Return (kind, reason): kind is device | dvd-folder | iso, or '' + why.

    Symlinks are resolved first so a neutral link name pointing at a VIDEO_TS
    folder or an .iso is classified by its target, not by the link.
    """
    if not raw.strip():
        return "", "empty path"
    text = raw.strip()
    stripped = text.rstrip("/\\")
    if stripped and not (len(stripped) == 2 and stripped[1] == ":"):
        text = stripped
    try:
        expanded = os.path.expanduser(text)
        path = Path(expanded)
        if not path.exists():
            if os.path.lexists(expanded):
                try:
                    os.path.realpath(expanded, strict=True)
                except OSError as error:
                    return "", f"cannot resolve symlink ({error.strerror or error})"
            return "", f"path does not exist: {path}"
        resolved = Path(os.path.realpath(expanded, strict=True))
    except (OSError, ValueError, RuntimeError) as error:
        return "", f"cannot resolve path ({error})"
    try:
        if resolved.is_dir():
            if resolved.name.lower() == "video_ts":
                if _has_ifo(resolved):
                    return "dvd-folder", ""
                return "", "VIDEO_TS folder has no VIDEO_TS.IFO or VIDEO_TS.BUP"
            if (resolved / "VIDEO_TS").is_dir():
                if _has_ifo(resolved / "VIDEO_TS"):
                    return "dvd-folder", ""
                return "", "folder has no VIDEO_TS/VIDEO_TS.IFO (not a DVD layout)"
            return "", "directory has no VIDEO_TS folder and is not one itself"
        if resolved.is_file():
            if resolved.suffix.lower() == ".iso":
                return "iso", ""
            return "", "unsupported file type (expected an .iso image)"
        try:
            mode = resolved.stat().st_mode
        except OSError as error:
            return "", f"cannot stat path ({error})"
        if stat.S_ISBLK(mode):
            return "device", ""
        return "", "not a block device, .iso image or VIDEO_TS folder"
    except OSError as error:
        return "", f"cannot inspect path ({error})"


def _scan_base(base: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            target = Path(os.path.realpath(entry, strict=True))
        except OSError:
            continue
        try:
            if target.is_dir():
                if _has_ifo(target / "VIDEO_TS") or (
                    target.name.lower() == "video_ts" and _has_ifo(target)
                ):
                    found.append(("dvd-folder", entry))
            elif target.is_file() and target.suffix.lower() == ".iso":
                found.append(("iso", entry))
        except OSError:
            continue
    return found


def scan_sources() -> tuple[list[tuple[str, Path]], int]:
    """Non-recursive scan of cwd and the usual media folders; cap the list."""
    bases: list[Path] = []
    try:
        bases.append(Path.cwd())
    except OSError:
        pass
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        home = None
    if home is not None:
        for name in SOURCE_DIRS[1:]:
            bases.append(home / name)
    seen: set[str] = set()
    found: list[tuple[str, Path]] = []
    for base in bases:
        for kind, path in _scan_base(base):
            try:
                key = os.path.realpath(path)
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            found.append((kind, path))
    return found[:MAX_SOURCES], max(0, len(found) - MAX_SOURCES)


def _state_label(device: str) -> str:
    state = disc_state(device)
    if state is True:
        return "disc ready"
    if state is False:
        return "empty (no disc)"
    return "unknown (cannot probe)"


def check_disc(report: Report) -> None:
    report.section("Disc locations")
    try:
        devices = optical_devices()
        print("  Optical drives (the app looks here by default):")
        if devices is None:
            print(
                "    unknown — no probe available on this system "
                "(install the OS optical tools, or pass --disc PATH)"
            )
        elif not devices:
            print("    none found — no optical drive was detected; pass --disc PATH")
        else:
            for device in devices[:MAX_DEVICES]:
                print(f"    {device} — {_state_label(device)}")
            hidden_devices = max(0, len(devices) - MAX_DEVICES)
            if hidden_devices:
                print(f"    ... and {hidden_devices} more not shown")
        sources, hidden = scan_sources()
        print("  Playable sources (cwd, ~/Videos, ~/Downloads, ~/Desktop):")
        if not sources:
            print("    none found — put a VIDEO_TS folder or .iso there, or pass --disc PATH")
        else:
            for kind, path in sources:
                print(f"    {kind} {path}")
        if hidden:
            print(f"    ... and {hidden} more not shown")
    except Exception:  # noqa: BLE001 - probing must never raise at install time
        print("    unknown — disc probing failed; pass --disc PATH to check a source")


def main(argv: list[str] | None = None) -> int:
    global _OS_OVERRIDE
    parser = argparse.ArgumentParser(
        prog="install.py",
        description=(
            "Verify and install DVD Player's dependencies. Common checks run first, "
            "then an explicit branch handles Linux, macOS or Windows. Linux is fully "
            "supported; macOS and Windows are experimental (folder/ISO playback only, "
            "optical handling is Linux-specific)."
        ),
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="skip `uv sync` and only verify the existing environment",
    )
    parser.add_argument(
        "--platform",
        choices=sorted(PLATFORM_ALIASES),
        default="auto",
        help=(
            "force an OS branch instead of detecting sys.platform "
            "(default: auto; darwin/win32 are accepted aliases)"
        ),
    )
    parser.add_argument(
        "--disc",
        metavar="PATH",
        default=None,
        help=(
            "validate and print where PATH will be played from: an optical "
            "device, a VIDEO_TS folder (or its parent) or an .iso file"
        ),
    )
    parser.add_argument(
        "--pause",
        action="store_true",
        help=(
            "wait for Enter before exiting; meant for the double-clicked "
            "Windows install-windows.exe so the window stays open"
        ),
    )
    args = parser.parse_args(argv)
    _OS_OVERRIDE = normalize_platform(args.platform)

    code = run(args)

    # Only a real terminal can answer: a piped or closed stdin must never block.
    if args.pause and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    return code


def run(args: argparse.Namespace) -> int:
    key = current_os()
    distro = distro_family()
    print("DVD Player installer — cross-platform dependency check")
    print(f"Repo: {REPO_ROOT}")
    print(f"Platform: {sys.platform}" + (f" ({distro})" if distro else ""))
    if _OS_OVERRIDE:
        print(f"Forced branch: {_OS_OVERRIDE} (--platform)")

    requested = getattr(args, "disc", None)
    if requested is not None:
        try:
            kind, reason = classify_source(requested)
        except Exception:  # noqa: BLE001 - validation must never traceback
            kind, reason = "", "cannot validate this path"
        if not kind:
            print(f"invalid --disc: {reason}", file=sys.stderr)
            return 1
        try:
            shown = Path(os.path.expanduser(requested.strip()))
            resolved = Path(os.path.realpath(shown))
        except (OSError, ValueError, RuntimeError):
            shown, resolved = Path(requested), None
        if resolved is not None and str(resolved) != str(shown):
            print(f"will play: {kind} {shown} -> {resolved}")
        else:
            print(f"will play: {kind} {shown}")

    report = Report()
    if not check_python(report):
        print("\nResult: FAIL — install Python 3.12+ first.")
        return 1

    uv_ok = check_uv(report)
    if uv_ok:
        check_sync(report, run_sync=not args.no_sync)
    else:
        report.warn("uv sync skipped", "uv is required to install Python packages")

    if venv_python() is not None or uv_ok:
        check_imports(report)

    report.section("Native libraries")
    mpv_names, dvdcss_names, dvdnav_names = native_names()
    check_native(report, mpv_names, "libmpv", hint_libmpv(), hard=True)
    check_native(report, dvdcss_names, "libdvdcss", hint_libdvdcss(), hard=False)
    check_native(report, dvdnav_names, "libdvdnav", hint_libdvdnav(), hard=False)

    PLATFORMS.get(key, platform_other)(report)

    check_disc(report)

    if report.failures:
        print(f"\nResult: FAIL — {report.failures} hard dependency problem(s) above.")
        return 1
    if report.warnings:
        print(f"\nResult: OK with {report.warnings} warning(s) — see hints above.")
    else:
        print("\nResult: OK — every check passed.")
    print("Run the player with: uv run dvd-player")
    return 0


if __name__ == "__main__":
    sys.exit(main())
