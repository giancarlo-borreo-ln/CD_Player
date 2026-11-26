@echo off
rem One-line installer for Windows. Prefers install-windows.exe next to this
rem file, otherwise falls back to Python. Wrong OS and missing Python: exit 2/1.
setlocal

if /i not "%OS%"=="Windows_NT" (
    echo install-windows.cmd: this installer is for Windows only. 1>&2
    echo Use ./install-linux.sh on Linux or ./install-mac.sh on macOS. 1>&2
    exit /b 2
)

set "HERE=%~dp0"

if exist "%HERE%install-windows.exe" (
    "%HERE%install-windows.exe" %*
    exit /b
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%HERE%scripts\install.py" --platform windows %*
    exit /b
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%HERE%scripts\install.py" --platform windows %*
    exit /b
)

echo install-windows.cmd: Python 3.12+ was not found. 1>&2
echo Install it, then re-run: winget install --id Python.Python.3.12 1>&2
exit /b 1
