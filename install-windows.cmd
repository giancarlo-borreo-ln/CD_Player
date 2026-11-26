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

rem Find Python 3.12+; the helper installs it with winget if missing (unattended;
rem pass --ask to be prompted first).
set "PYFILE=%TEMP%\dvd-player-python-%RANDOM%-%RANDOM%.txt"
set "BOOTMODE=-AssumeYes"
if not "%*:--ask=%"=="%*" set "BOOTMODE=-Ask"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%HERE%\scripts\install-windows-bootstrap.ps1" -Repo "%HERE%" -OutFile "%PYFILE%" %BOOTMODE%
if errorlevel 1 (
    if exist "%PYFILE%" del /q "%PYFILE%" >nul 2>nul
    exit /b 1
)
set "PYTHON="
set /p PYTHON=<"%PYFILE%"
del /q "%PYFILE%" >nul 2>nul
if not defined PYTHON (
    echo install-windows.cmd: Python 3.12+ was not found. 1>&2
    exit /b 1
)

set "ARGS=%*"
set "MODE=--install"
set "LAUNCH=1"
if not "%ARGS:--install=%"=="%ARGS%" set "MODE="
if not "%ARGS:--check=%"=="%ARGS%" set "MODE="
if not "%ARGS:--check=%"=="%ARGS%" set "LAUNCH="
if not "%ARGS:--uninstall=%"=="%ARGS%" set "MODE="
if not "%ARGS:--uninstall=%"=="%ARGS%" set "LAUNCH="

"%PYTHON%" "%HERE%\scripts\install.py" --platform windows %MODE% %*
if errorlevel 1 exit /b %ERRORLEVEL%

if defined LAUNCH (
    if exist "%USERPROFILE%\.local\bin\dvd-player.exe" (
        set "PATH=%LOCALAPPDATA%\dvd-player\lib;%PATH%"
        start "" "%USERPROFILE%\.local\bin\dvd-player.exe"
    ) else (
        echo install-windows.cmd: dvd-player.exe was not found after install. 1>&2
        echo Run: uv tool install --force "%HERE%" 1>&2
        exit /b 1
    )
)
exit /b 0
