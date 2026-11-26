#!/usr/bin/env sh
# One-line installer for Linux. Delegates to scripts/install.py.
# Wrong OS is refused with exit code 2; missing Python exits 1.
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OS_NAME=$(uname -s 2>/dev/null || echo unknown)

case "$OS_NAME" in
    Linux*) ;;
    *)
        printf '%s\n' \
            "install-linux.sh: this installer is for Linux, but uname reports '$OS_NAME'." \
            "Use ./install-mac.sh on macOS or install-windows.cmd on Windows." >&2
        exit 2
        ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' \
        "install-linux.sh: python3 not found." \
        "Install Python 3.12+ with your package manager, then re-run this script." >&2
    exit 1
fi

exec python3 "$REPO_DIR/scripts/install.py" --platform linux "$@"
