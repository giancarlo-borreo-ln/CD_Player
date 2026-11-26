#!/usr/bin/env python3
"""Build the Windows one-file installer, install-windows.exe.

Standard library only. This is a release-time tool and MUST run on Windows:
PyInstaller cannot cross-compile from Linux or macOS. The executable bakes in
``--platform windows --pause`` via scripts/entry_exe.py.

    py -3 scripts/build_windows_exe.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY = REPO_ROOT / "scripts" / "entry_exe.py"
EXE_NAME = "install-windows"


def honesty_error() -> int:
    print(
        "build_windows_exe.py: refusing to build.\n"
        "PyInstaller cannot cross-compile; build on Windows.\n"
        "On a Windows machine run: py -3 scripts/build_windows_exe.py",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    if sys.platform != "win32":
        return honesty_error()

    if shutil.which("py") is None and shutil.which("python") is None:
        print("build_windows_exe.py: Python not found on PATH.", file=sys.stderr)
        return 1

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--console",
            "--noconfirm",
            "--clean",
            "--name",
            EXE_NAME,
            "--distpath",
            str(REPO_ROOT / "dist"),
            "--workpath",
            str(REPO_ROOT / "build" / "installer"),
            "--specpath",
            str(REPO_ROOT / "build" / "installer"),
            str(ENTRY),
        ],
        check=False,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        print(
            "build_windows_exe.py: PyInstaller failed; install it with "
            "`py -3 -m pip install pyinstaller` and retry.",
            file=sys.stderr,
        )
        return result.returncode or 1

    exe = REPO_ROOT / "dist" / f"{EXE_NAME}.exe"
    if not exe.exists():
        print(f"build_windows_exe.py: expected {exe} was not produced.", file=sys.stderr)
        return 1
    print(f"Built: {exe}")
    print("Copy it beside install-windows.cmd; do not commit it (release artifact).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
