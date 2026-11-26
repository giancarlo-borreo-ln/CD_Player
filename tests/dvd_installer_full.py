"""Headless installer tests with fake binaries (no network, no 7-Zip).

Run: python3 tests/dvd_installer_full.py
"""
from __future__ import annotations

import builtins
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import install

_spec = importlib.util.spec_from_file_location(
    "dvd_player_win_dll", REPO / "src" / "dvd_player" / "_win_dll.py"
)
assert _spec is not None and _spec.loader is not None
_win_dll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_win_dll)

COUNT = 0


def check(condition: object) -> None:
    global COUNT
    assert condition
    COUNT += 1


def test_winget_preferred_without_7z() -> None:
    root = Path(tempfile.mkdtemp())
    local = root / "local"
    package = local / "Microsoft" / "WinGet" / "Packages" / "shinchiro.mpv_1"
    package.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], cwd: Path | None = None, capture: bool = False):
        calls.append(list(argv))
        if argv[0] == "winget":
            (package / "libmpv-2.dll").write_bytes(b"dll")
        return install.subprocess.CompletedProcess(argv, 0, "", "")

    def fake_which(name: str) -> str | None:
        return "winget" if name == "winget" else None

    old_run, old_which = install.run_argv, install.shutil.which
    old_consent = install.consent
    old_env = os.environ.get("LOCALAPPDATA")
    try:
        install.run_argv = fake_run
        install.shutil.which = fake_which
        install.consent = lambda assume, message: True
        os.environ["LOCALAPPDATA"] = str(local)
        report = install.Report()
        ok = install.install_windows_native(report, True, None)
        check(ok)
        check(report.failures == 0)
        check(calls and calls[0][0] == "winget")
        check("--id" in calls[0] and "mpv" in calls[0])
        check(all(call[0] != sys.executable for call in calls))
    finally:
        install.run_argv, install.shutil.which = old_run, old_which
        install.consent = old_consent
        if old_env is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_env


def test_no_winget_no_extractor_fails_closed() -> None:
    old_which, old_consent = install.shutil.which, install.consent
    try:
        install.shutil.which = lambda name: None
        install.consent = lambda assume, message: True
        report = install.Report()
        ok = install.install_windows_native(report, True, None)
        check(not ok)
        check(report.failures == 1)
    finally:
        install.shutil.which, install.consent = old_which, old_consent


def test_add_dll_directory_dirs_and_missing() -> None:
    root = Path(tempfile.mkdtemp())
    local = root / "local"
    lib = local / "dvd-player" / "lib"
    lib.mkdir(parents=True)
    package = local / "Microsoft" / "WinGet" / "Packages" / "mpv_1" / "bin"
    package.mkdir(parents=True)
    (package / "libmpv-2.dll").write_bytes(b"dll")
    exe_dir = root / "tool"
    exe_dir.mkdir()
    (exe_dir / "dvd-player.exe").write_bytes(b"exe")
    called: list[str] = []

    registered = _win_dll.register(
        platform_name="win32",
        environ={
            "LOCALAPPDATA": str(local),
            "ProgramFiles": str(root / "missing-programs"),
            "PATH": str(lib),
        },
        executable=str(exe_dir / "dvd-player.exe"),
        add_dll_directory=called.append,
    )
    check(str(lib) in called)
    check(str(package) in called)
    check(str(exe_dir) in called)
    check(str(lib) in registered)

    empty = _win_dll.register(
        platform_name="win32",
        environ={},
        executable=str(root / "nope" / "python.exe"),
        add_dll_directory=lambda path: (_ for _ in ()).throw(OSError(path)),
    )
    check(empty == [])
    check(_win_dll.register(platform_name="linux") == [])


def test_manifest_hash_well_formed() -> None:
    import json

    manifest = json.loads((REPO / "scripts" / "deps-manifest.json").read_text())
    entry = manifest["deps"]["windows-amd64"][0]
    check(entry["status"] == "pinned")
    check(len(entry["sha256"]) == 64)
    int(entry["sha256"], 16)


def test_bootstrap_consent_defaults_true_without_prompt() -> None:
    old_tty, old_input = install.is_tty, builtins.input
    prompts: list[str] = []

    def forbidden_input(prompt: str) -> str:
        prompts.append(prompt)
        raise AssertionError("bootstrap consent must not prompt by default")

    try:
        install.is_tty = lambda: True
        builtins.input = forbidden_input
        check(install.consent(False, "install uv with winget?") is True)
        check(prompts == [])
    finally:
        install.is_tty, builtins.input = old_tty, old_input


def test_bootstrap_ask_prompts() -> None:
    old_tty, old_input = install.is_tty, builtins.input
    prompts: list[str] = []

    try:
        install.is_tty = lambda: True

        def decline(prompt: str) -> str:
            prompts.append(prompt)
            return "n"

        builtins.input = decline
        check(install.consent(True, "install mpv?") is False)
        check(prompts == ["install mpv? [y/N] "])

        builtins.input = lambda prompt: "y"
        check(install.consent(True, "install mpv?") is True)
    finally:
        install.is_tty, builtins.input = old_tty, old_input


def test_windows_native_missing_stubs_does_not_prompt() -> None:
    old_which, old_tty, old_input = install.shutil.which, install.is_tty, builtins.input

    def forbidden_input(prompt: str) -> str:
        raise AssertionError("missing stubs must not prompt without --ask")

    try:
        install.shutil.which = lambda name: None
        install.is_tty = lambda: True
        builtins.input = forbidden_input
        report = install.Report()
        ok = install.install_windows_native(report, False, None)
        check(not ok)
        check(report.failures == 1)
    finally:
        install.shutil.which, install.is_tty, builtins.input = old_which, old_tty, old_input


def test_windows_native_ask_prompts_on_missing_mpv() -> None:
    old_which, old_tty, old_input = install.shutil.which, install.is_tty, builtins.input
    prompts: list[str] = []

    def decline(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    try:
        install.shutil.which = lambda name: "tar" if name == "tar" else None
        install.is_tty = lambda: True
        builtins.input = decline
        report = install.Report()
        ok = install.install_windows_native(report, True, None)
        check(not ok)
        check(len(prompts) == 1)
        check("pinned libmpv" in prompts[0])
    finally:
        install.shutil.which, install.is_tty, builtins.input = old_which, old_tty, old_input


for test in (
    test_winget_preferred_without_7z,
    test_no_winget_no_extractor_fails_closed,
    test_bootstrap_consent_defaults_true_without_prompt,
    test_bootstrap_ask_prompts,
    test_windows_native_missing_stubs_does_not_prompt,
    test_windows_native_ask_prompts_on_missing_mpv,
    test_add_dll_directory_dirs_and_missing,
    test_manifest_hash_well_formed,
):
    test()

print(f"OK {COUNT} checks")
