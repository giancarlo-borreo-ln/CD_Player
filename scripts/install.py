#!/usr/bin/env python3
"""Cross-platform installer/checker for DVD Player.

Standard library only. Common checks run first (Python, uv, project env,
imports, native libraries); then one explicit platform branch handles the
OS-specific setup: Linux, macOS or Windows.

    python3 scripts/install.py          # check + `uv sync`
    python3 scripts/install.py --no-sync

Exit codes: 0 = ready to run, 1 = a hard dependency is missing.
"""

from __future__ import annotations

import argparse
import ctypes.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 12)


def current_os() -> str:
    """Map sys.platform to a branch key: linux, macos, windows or other."""
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


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    key = current_os()
    distro = distro_family()
    print("DVD Player installer — cross-platform dependency check")
    print(f"Repo: {REPO_ROOT}")
    print(f"Platform: {sys.platform}" + (f" ({distro})" if distro else ""))

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
