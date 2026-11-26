"""Headless installer tests with fake binaries (no network, no 7-Zip).

Run: python3 tests/dvd_installer_full.py
"""
from __future__ import annotations

import argparse
import builtins
import contextlib
import importlib.util
import io
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
    check(entry["github_asset_digest"] == entry["sha256"])
    names = [
        dep["name"]
        for deps in manifest["deps"].values()
        for dep in deps
    ]
    check(not any("dvdcss" in name.lower() for name in names))
    check(
        not any(dep.get("status") == "unverified" for deps in manifest["deps"].values() for dep in deps)
    )
    check(
        not any(
            "dvdcss" in (dep.get("url") or "").lower()
            for deps in manifest["deps"].values()
            for dep in deps
        )
    )


def test_windows_installers_are_independent_and_map_flags() -> None:
    text = (REPO / "install-windows.ps1").read_text(encoding="utf-8")
    cmd = (REPO / "install-windows.cmd").read_text(encoding="utf-8")
    lowered = text.lower()
    check("setlocal" not in lowered)
    check("@echo" not in lowered)
    check("%*" not in text)
    check(text.count("{") == text.count("}"))
    check(text.count("(") == text.count(")"))
    check(lowered.lstrip().startswith("#requires"))
    _, remainder = text.split("param(", 1)
    param_block = remainder.split("\n)", 1)[0]
    check(param_block.count("(") == param_block.count(")"))
    for name in ("$Ask", "$Yes", "$Check", "$Uninstall", "$Purge"):
        check(name in param_block)
    check("install-windows.ps1" not in cmd)
    check("scripts\\install.py" in cmd)
    flags = ("--install", "--ask", "--check", "--uninstall", "--purge")
    for flag in flags:
        check(flag in text)
        check(flag in cmd)
    check("--platform" in text and "'windows'" in text)
    check("--platform windows" in cmd)


def test_windows_launch_exit_propagation_and_notice() -> None:
    ps1 = (REPO / "install-windows.ps1").read_text(encoding="utf-8")
    cmd = (REPO / "install-windows.cmd").read_text(encoding="utf-8")
    ps1_lowered = ps1.lower()
    cmd_lowered = cmd.lower()
    notice = (
        "windows support is verified statically, not on real hardware; "
        "encrypted-dvd playback depends on the libmpv build; "
        "start menu indexing may lag."
    )
    check(cmd_lowered.count(notice) == 1)
    check(ps1_lowered.count(notice) == 1)
    check('start "" /wait' in cmd_lowered)
    check("if not errorlevel 1 exit /b 0" in cmd_lowered)
    check("%localappdata%\\dvd-player\\dvd-player.log" in cmd_lowered)
    check(
        "start-process -filepath $exe -workingdirectory $repo -wait -passthru"
        in ps1_lowered
    )
    check("$process.exitcode" in ps1_lowered)
    check("dvd-player\\dvd-player.log" in ps1_lowered)


def test_failure_after_tool_install_rolls_back_new_tool() -> None:
    root = Path(tempfile.mkdtemp())
    shortcut = root / "DVD Player.lnk"
    exe = root / "dvd-player.exe"
    exe.write_bytes(b"exe")
    calls: list[list[str]] = []
    old = (
        install.ensure_python, install.check_python, install.ensure_uv,
        install.install_native, install.install_tool, install.snapshot_install_state,
        install.tool_binary, install.create_entry, install.smoke_test,
        install.shutil.which, install.run_argv, install.check_native,
        install.check_disc,
    )

    def fake_run(argv: list[str], cwd: Path | None = None, capture: bool = False):
        calls.append(list(argv))
        return install.subprocess.CompletedProcess(argv, 0, "", "")

    def fake_create_entry(report: install.Report, binary: Path) -> bool:
        shortcut.write_text("lnk", encoding="utf-8")
        report.fail("Start Menu shortcut failed", "injected failure")
        return False

    try:
        install.ensure_python = lambda report, ask: True
        install.check_python = lambda report: True
        install.ensure_uv = lambda report, ask: True
        install.install_native = lambda report, ask, offline, dvdcss=None: True
        install.snapshot_install_state = lambda: {
            "tool_existed": False,
            "entries": {str(shortcut): False},
        }
        install.install_tool = lambda report, offline: True
        install.tool_binary = lambda: exe
        install.create_entry = fake_create_entry
        install.smoke_test = lambda report: True
        install.shutil.which = lambda name: "uv" if name == "uv" else None
        install.run_argv = fake_run
        install.check_native = lambda *args, **kwargs: None
        install.check_disc = lambda report: None
        code = install.run(_install_args())
    finally:
        (
            install.ensure_python, install.check_python, install.ensure_uv,
            install.install_native, install.install_tool, install.snapshot_install_state,
            install.tool_binary, install.create_entry, install.smoke_test,
            install.shutil.which, install.run_argv, install.check_native,
            install.check_disc,
        ) = old
    check(code == 1)
    check(not shortcut.exists())
    check(["uv", "tool", "uninstall", "dvd-player"] in calls)


def test_rollback_keeps_preexisting_tool_and_shortcut() -> None:
    root = Path(tempfile.mkdtemp())
    shortcut = root / "DVD Player.lnk"
    shortcut.write_text("lnk", encoding="utf-8")
    calls: list[list[str]] = []
    old_which, old_run = install.shutil.which, install.run_argv
    try:
        install.shutil.which = lambda name: "uv" if name == "uv" else None
        install.run_argv = lambda argv, cwd=None, capture=False: (
            calls.append(list(argv))
            or install.subprocess.CompletedProcess(argv, 0, "", "")
        )
        report = install.Report()
        install.rollback_install(
            report, {"tool_existed": True, "entries": {str(shortcut): True}}
        )
    finally:
        install.shutil.which, install.run_argv = old_which, old_run
    check(calls == [])
    check(shortcut.exists())
    check(report.failures == 0)


def test_windows_uv_first_flow_without_system_python() -> None:
    """Fakes winget/uv: uv is resolved or installed silently, no system Python."""
    cmd = (REPO / "install-windows.cmd").read_text(encoding="utf-8")
    ps1 = (REPO / "install-windows.ps1").read_text(encoding="utf-8")
    lowered = cmd.lower()
    ps1_lowered = ps1.lower()
    check((REPO / ".python-version").read_text(encoding="utf-8").strip() == "3.13")

    # No prompts and no system Python bootstrap in the batch launcher.
    check("set /p" not in lowered)
    check("powershell" not in lowered)
    check("read-host" not in ps1_lowered)
    check("python.exe" not in lowered)
    check("programs\\python" not in lowered)
    for text in (lowered, ps1_lowered):
        check("astral-sh.uv" in text)
        check("--disable-interactivity" in text)
        check("python install 3.13" in text)
        check("--python" in text and "3.13" in text)

    # uv resolution order: PATH, winget links, uv's own bin, cargo bin.
    check("where uv" in lowered)
    check("microsoft\\winget\\links\\uv.exe" in lowered)
    check("userprofile%\\.local\\bin\\uv.exe" in lowered)
    check("userprofile%\\.cargo\\bin\\uv.exe" in lowered)

    # Missing uv -> a single silent winget install -> re-resolve -> uv run.
    winget_line = (
        "winget install --id astral-sh.uv --exact --silent "
        "--accept-package-agreements --accept-source-agreements "
        "--disable-interactivity"
    )
    check(winget_line in lowered)
    install_at = lowered.index('"%uv%" python install 3.13')
    run_at = lowered.index('"%uv%" run --python 3.13')
    first_lookup = lowered.index("call :find_uv")
    check(first_lookup < lowered.index(winget_line) < install_at < run_at)
    check("where uv" in lowered[first_lookup:])
    check("scripts\\install.py" in lowered[run_at:])

    # Launch only after install.py exits 0, and never for check/uninstall.
    failed_at = lowered.index("if errorlevel 1 exit /b %errorlevel%", run_at)
    launch_at = lowered.index("if defined launch", failed_at)
    check(run_at < failed_at < launch_at)
    check('start ""' in lowered[launch_at:])
    check('"%args:--check=%"=="%args%" set "launch="' in lowered)
    check('"%args:--uninstall=%"=="%args%" set "launch="' in lowered)

    # The PowerShell entry point follows the same uv-first flow.
    check("function find-uv" in ps1_lowered)
    check("function install-uv" in ps1_lowered)
    check("& $uv python install 3.13" in ps1)
    check("$installargs.add('run')" in ps1_lowered)
    check("find-python" not in ps1_lowered)
    check("test-python" not in ps1_lowered)


def _fake_winget_libmpv(payload: bytes) -> tuple[bool, install.Report, list[list[str]], str]:
    root = Path(tempfile.mkdtemp())
    local = root / "local"
    package = local / "Microsoft" / "WinGet" / "Packages" / "shinchiro.mpv_1"
    package.mkdir(parents=True)
    (package / "libmpv-2.dll").write_bytes(payload)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], cwd: Path | None = None, capture: bool = False):
        calls.append(list(argv))
        return install.subprocess.CompletedProcess(argv, 0, "", "")

    old_run, old_which = install.run_argv, install.shutil.which
    old_consent = install.consent
    old_env = os.environ.get("LOCALAPPDATA")
    output = io.StringIO()
    try:
        install.run_argv = fake_run
        install.shutil.which = lambda name: "winget" if name == "winget" else None
        install.consent = lambda assume, message: True
        os.environ["LOCALAPPDATA"] = str(local)
        report = install.Report()
        with contextlib.redirect_stdout(output):
            ok = install.install_windows_native(report, True, None)
    finally:
        install.run_argv, install.shutil.which = old_run, old_which
        install.consent = old_consent
        if old_env is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_env
    return ok, report, calls, output.getvalue()


def test_bundled_libdvdcss_reports_ok() -> None:
    ok, report, calls, output = _fake_winget_libmpv(
        b"prefix-DVDCSS_METHOD-suffix"
    )
    check(ok)
    check(report.failures == 0)
    check(report.warnings == 0)
    check("bundled in libmpv" in output)
    check(all("dvdcss" not in " ".join(call).lower() for call in calls))
    check(all(call[0] != sys.executable for call in calls))


def test_missing_dvdcss_marker_warns_once_without_fetch() -> None:
    ok, report, calls, output = _fake_winget_libmpv(b"plain libmpv bytes")
    check(ok)
    check(report.failures == 0)
    check(report.warnings == 1)
    check("--dvdcss PATH" in output)
    check(all("dvdcss" not in " ".join(call).lower() for call in calls))
    check(all(call[0] != sys.executable for call in calls))


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


def _uninstall_args(purge: bool = False, yes: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        mode="uninstall", purge=purge, yes=yes, ask=False, disc=None, offline=None
    )


def test_non_tty_default_mode_is_install() -> None:
    old_tty, old_run = install.is_tty, install.run
    seen: list[str] = []

    def fake_run(args: argparse.Namespace) -> int:
        seen.append(args.mode)
        return 0

    try:
        install.is_tty = lambda: False
        install.run = fake_run
        check(install.main([]) == 0)
        check(seen == ["install"])
    finally:
        install.is_tty, install.run = old_tty, old_run


def test_uninstall_non_tty_requires_yes() -> None:
    old_tty, old_uninstall = install.is_tty, install.uninstall
    calls: list[bool] = []

    def fake_uninstall(report: object, purge: bool) -> int:
        calls.append(purge)
        return 0

    try:
        install.is_tty = lambda: False
        install.uninstall = fake_uninstall
        check(install.run(_uninstall_args(purge=True, yes=False)) == 1)
        check(calls == [])
        check(install.run(_uninstall_args(purge=True, yes=True)) == 0)
        check(calls == [True])
    finally:
        install.is_tty, install.uninstall = old_tty, old_uninstall


def test_uninstall_tty_always_confirms_even_with_yes() -> None:
    old_tty, old_input, old_uninstall = install.is_tty, builtins.input, install.uninstall
    calls: list[bool] = []

    def fake_uninstall(report: object, purge: bool) -> int:
        calls.append(purge)
        return 0

    try:
        install.is_tty = lambda: True
        install.uninstall = fake_uninstall
        builtins.input = lambda prompt: "n"
        check(install.run(_uninstall_args(purge=False, yes=True)) == 1)
        check(calls == [])
        builtins.input = lambda prompt: "y"
        check(install.run(_uninstall_args(purge=False, yes=True)) == 0)
        check(calls == [False])
    finally:
        install.is_tty, builtins.input, install.uninstall = old_tty, old_input, old_uninstall


def _install_args() -> argparse.Namespace:
    return argparse.Namespace(
        mode="install", purge=False, yes=False, ask=False, disc=None,
        offline=None, no_sync=False,
    )


def test_native_failure_aborts_before_install_tool() -> None:
    old = (
        install.ensure_python, install.check_python, install.ensure_uv,
        install.install_native, install.install_tool,
    )
    calls: list[str] = []
    try:
        install.ensure_python = lambda report, ask: True
        install.check_python = lambda report: True
        install.ensure_uv = lambda report, ask: True
        install.install_native = lambda report, ask, offline, dvdcss=None: False
        install.install_tool = lambda report, offline: calls.append("tool") or True
        check(install.run(_install_args()) == 1)
        check(calls == [])
    finally:
        (
            install.ensure_python, install.check_python, install.ensure_uv,
            install.install_native, install.install_tool,
        ) = old


def _native_stubs(rc_for_optional: int):
    old = (
        install.current_os, install.distro_family, install.shutil.which,
        install.run_argv, install.with_privilege, install.consent,
    )
    calls: list[list[str]] = []

    def fake_run(argv: list[str], cwd: Path | None = None, capture: bool = False):
        calls.append(list(argv))
        rc = rc_for_optional if "libdvdcss2" in argv else 0
        return install.subprocess.CompletedProcess(argv, rc, "", "")

    install.current_os = lambda: "linux"
    install.distro_family = lambda: "debian"
    install.shutil.which = lambda name: "/usr/bin/apt-get" if name == "apt-get" else None
    install.run_argv = fake_run
    install.with_privilege = lambda argv: argv
    install.consent = lambda ask, message: True
    return old, calls


def _restore_native_stubs(old: tuple) -> None:
    (
        install.current_os, install.distro_family, install.shutil.which,
        install.run_argv, install.with_privilege, install.consent,
    ) = old


def test_optional_libdvdcss_failure_is_warning_only() -> None:
    old, calls = _native_stubs(rc_for_optional=100)
    try:
        report = install.Report()
        check(install.install_native(report, False, None) is True)
        check(report.failures == 0)
        check(report.warnings == 1)
        check(len(calls) == 2)
        check("libdvdcss2" not in calls[1])
    finally:
        _restore_native_stubs(old)


def test_required_native_failure_is_hard() -> None:
    old, calls = _native_stubs(rc_for_optional=0)
    try:
        install.run_argv = lambda argv, cwd=None, capture=False: (
            calls.append(list(argv))
            or install.subprocess.CompletedProcess(argv, 100, "", "")
        )
        report = install.Report()
        check(install.install_native(report, False, None) is False)
        check(report.failures == 1)
        check(len(calls) == 2)
    finally:
        _restore_native_stubs(old)


for test in (
    test_winget_preferred_without_7z,
    test_no_winget_no_extractor_fails_closed,
    test_bootstrap_consent_defaults_true_without_prompt,
    test_bootstrap_ask_prompts,
    test_windows_native_missing_stubs_does_not_prompt,
    test_windows_native_ask_prompts_on_missing_mpv,
    test_non_tty_default_mode_is_install,
    test_uninstall_non_tty_requires_yes,
    test_uninstall_tty_always_confirms_even_with_yes,
    test_native_failure_aborts_before_install_tool,
    test_optional_libdvdcss_failure_is_warning_only,
    test_required_native_failure_is_hard,
    test_add_dll_directory_dirs_and_missing,
    test_manifest_hash_well_formed,
    test_windows_installers_are_independent_and_map_flags,
    test_windows_launch_exit_propagation_and_notice,
    test_failure_after_tool_install_rolls_back_new_tool,
    test_rollback_keeps_preexisting_tool_and_shortcut,
    test_windows_uv_first_flow_without_system_python,
    test_bundled_libdvdcss_reports_ok,
    test_missing_dvdcss_marker_warns_once_without_fetch,
):
    test()

print(f"OK {COUNT} checks")
