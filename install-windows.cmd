@echo off
rem One-line installer for Windows: Python (if missing), uv, pinned libmpv,
rem the dvd-player tool, a Start Menu shortcut, then launches the player.
rem Uninstall: install-windows.cmd --uninstall [--purge]
rem Wrong OS exits 2; bootstrap failure exits 1.
setlocal EnableExtensions

if /i not "%OS%"=="Windows_NT" (
    echo install-windows.cmd: this installer is for Windows only. 1>&2
    echo Use ./install-linux.sh on Linux or ./install-mac.sh on macOS. 1>&2
    exit /b 2
)

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

if exist "%HERE%\install-windows.exe" (
    "%HERE%\install-windows.exe" %*
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%\install-windows.ps1" %*
exit /b %ERRORLEVEL%
