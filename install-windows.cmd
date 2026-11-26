@echo off
rem One-line installer for Windows: uv (installed with winget if missing),
rem a uv-managed CPython 3.13, pinned libmpv, the dvd-player tool, a Start
rem Menu shortcut, then launches the player. No system Python is required.
rem Uninstall: install-windows.cmd --uninstall [--purge]
rem Wrong OS exits 2; a missing uv/winget exits 1.
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
    exit /b %ERRORLEVEL%
)

rem Find uv: PATH first, then the usual winget, uv and cargo locations.
call :find_uv
if not defined UV (
    where winget >nul 2>nul
    if errorlevel 1 (
        echo install-windows.cmd: uv is missing and winget is not available. 1>&2
        echo Install uv from https://docs.astral.sh/uv/ and re-run. 1>&2
        exit /b 1
    )
    rem Unattended: never prompt; --ask only affects install.py's own steps.
    winget install --id astral-sh.uv --exact --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    if errorlevel 1 (
        echo install-windows.cmd: winget could not install uv. 1>&2
        echo Install uv from https://docs.astral.sh/uv/ and re-run. 1>&2
        exit /b 1
    )
    call :find_uv
    if not defined UV (
        echo install-windows.cmd: uv was installed but is not visible yet. 1>&2
        echo Open a new terminal and re-run this installer. 1>&2
        exit /b 1
    )
)

rem Let uv provide CPython 3.13; no system Python is needed.
"%UV%" python install 3.13
if errorlevel 1 exit /b %ERRORLEVEL%

set "ARGS=%*"
set "MODE=--install"
set "LAUNCH=1"
if not "%ARGS:--install=%"=="%ARGS%" set "MODE="
if not "%ARGS:--check=%"=="%ARGS%" set "MODE="
if not "%ARGS:--check=%"=="%ARGS%" set "LAUNCH="
if not "%ARGS:--uninstall=%"=="%ARGS%" set "MODE="
if not "%ARGS:--uninstall=%"=="%ARGS%" set "LAUNCH="

"%UV%" run --python 3.13 "%HERE%\scripts\install.py" --platform windows %MODE% %*
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

:find_uv
set "UV="
for /f "delims=" %%I in ('where uv 2^>nul') do (
    if not defined UV set "UV=%%I"
)
if defined UV exit /b 0
for %%P in (
    "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
    "%USERPROFILE%\.local\bin\uv.exe"
    "%USERPROFILE%\.cargo\bin\uv.exe"
) do (
    if not defined UV if exist "%%~P" set "UV=%%~P"
)
exit /b 0
