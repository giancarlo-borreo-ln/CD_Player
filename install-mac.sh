#!/usr/bin/env sh
# One-line installer for macOS. Delegates to scripts/install.py.
# Wrong OS is refused with exit code 2; missing Python exits 1.
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OS_NAME=$(uname -s 2>/dev/null || echo unknown)

case "$OS_NAME" in
    Darwin) ;;
    *)
        printf '%s\n' \
            "install-mac.sh: this installer is for macOS, but uname reports '$OS_NAME'." \
            "Use ./install-linux.sh on Linux or install-windows.cmd on Windows." >&2
        exit 2
        ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' \
        "install-mac.sh: python3 not found." \
        "Install it with: brew install python@3.12" >&2
    exit 1
fi

exec python3 "$REPO_DIR/scripts/install.py" --platform macos "$@"
