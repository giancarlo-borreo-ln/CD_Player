"""Register Windows DLL search directories for libmpv.

Imported before ``mpv`` so ``libmpv-2.dll`` can be found in the per-user lib
folder, next to the executable, under Program Files\\mpv or in WinGet package
directories. Every probe is guarded: a missing folder never raises.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path


def _walk_dirs(root: Path) -> list[Path]:
    found: list[Path] = []
    try:
        if not root.is_dir():
            return found
        found.append(root)
        for match in root.rglob("libmpv-2.dll"):
            try:
                if match.is_file():
                    found.append(match.parent)
            except OSError:
                continue
    except OSError:
        pass
    return found


def candidate_dirs(
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
) -> list[str]:
    """Directories that may hold libmpv-2.dll, in priority order."""
    source = os.environ if environ is None else environ
    dirs: list[Path] = []
    local = source.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "dvd-player" / "lib")
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        try:
            for entry in packages.iterdir():
                if entry.is_dir() and "mpv" in entry.name.lower():
                    dirs.extend(_walk_dirs(entry))
        except OSError:
            pass
    for base in (source.get("ProgramFiles"), source.get("ProgramFiles(x86)")):
        if base:
            dirs.extend(_walk_dirs(Path(base) / "mpv"))
    exe = executable if executable is not None else sys.executable
    if exe:
        dirs.append(Path(exe).resolve().parent)
    for raw in source.get("PATH", "").split(os.pathsep):
        if raw:
            dirs.append(Path(raw))

    unique: list[str] = []
    seen: set[str] = set()
    for directory in dirs:
        try:
            key = str(directory).lower()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(str(directory))
    return unique


def register(
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
    add_dll_directory: Callable[[str], object] | None = None,
) -> list[str]:
    """Add every existing candidate directory to the DLL search path.

    Returns the directories actually registered. Never raises: a missing
    directory, a locked path or a non-Windows platform is ignored.
    """
    name = sys.platform if platform_name is None else platform_name
    if not name.startswith("win"):
        return []
    adder = add_dll_directory
    if adder is None:
        adder = getattr(os, "add_dll_directory", None)
    if adder is None:
        return []
    registered: list[str] = []
    for directory in candidate_dirs(environ, executable):
        try:
            if not Path(directory).is_dir():
                continue
            adder(directory)
            registered.append(directory)
        except (OSError, ValueError):
            continue
    if registered:
        try:
            current = os.environ.get("PATH", "")
            prefix = os.pathsep.join(registered)
            if current:
                os.environ["PATH"] = prefix + os.pathsep + current
            else:
                os.environ["PATH"] = prefix
        except OSError:
            pass
    return registered


# Importing this module is enough: it runs before ``mpv`` is imported.
_REGISTERED: list[str] = []
try:
    _REGISTERED = register()
except Exception:  # noqa: BLE001 - startup must never fail on DLL discovery
    _REGISTERED = []
