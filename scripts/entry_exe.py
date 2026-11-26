"""Frozen entry point for install-windows.exe.

PyInstaller picks this module up, follows ``import install`` and bakes the
Windows branch plus the pause prompt into the single-file executable. The
script lives beside install.py, so the import resolves at build time.
"""

from __future__ import annotations

import sys

import install

if __name__ == "__main__":
    sys.exit(install.main(["--platform", "windows", "--pause", *sys.argv[1:]]))
