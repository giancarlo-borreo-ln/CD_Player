#!/usr/bin/env sh
# One-line installer for macOS: Python (if missing), uv, mpv/libdvdcss, the
# dvd-player tool, a ~/Applications bundle, then launches the player.
# Uninstall: ./install-mac.sh --uninstall [--purge]
# Wrong OS is refused with exit code 2; a failed bootstrap exits 1.
set -eu

# Resolve the physical script path through symlinks (POSIX: macOS readlink has
# no -f). Works from any cwd and with spaces/unicode in the path.
SCRIPT=$0
HOPS=0
while [ -h "$SCRIPT" ]; do
    HOPS=$((HOPS + 1))
    if [ "$HOPS" -ge 40 ]; then
        printf '%s\n' \
            "install-mac.sh: too many symbolic links resolving $0" \
            " (stopped after $HOPS hops; the link chain may be cyclic)." >&2
        exit 2
    fi
    LINK_DIR=$(CDPATH= cd -P "$(dirname "$SCRIPT")" && pwd)
    if command -v readlink >/dev/null 2>&1; then
        TARGET=$(readlink "$SCRIPT") || break
    elif [ -x /usr/bin/readlink ]; then
        TARGET=$(/usr/bin/readlink "$SCRIPT") || break
    elif [ -x /bin/readlink ]; then
        TARGET=$(/bin/readlink "$SCRIPT") || break
    else
        break
    fi
    case $TARGET in
        /*) SCRIPT=$TARGET ;;
        *) SCRIPT=$LINK_DIR/$TARGET ;;
    esac
done
REPO_DIR=$(CDPATH= cd -P "$(dirname "$SCRIPT")" && pwd)
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

python_ok() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
        >/dev/null 2>&1
}

find_python() {
    for candidate in python3.13 python3.12 python3; do
        path=$(command -v "$candidate" 2>/dev/null) || continue
        if python_ok "$path"; then
            printf '%s\n' "$path"
            return 0
        fi
    done
    return 1
}

PYTHON=$(find_python) || true
if [ -z "${PYTHON:-}" ]; then
    if ! command -v brew >/dev/null 2>&1; then
        printf '%s\n' \
            "install-mac.sh: Python 3.12+ not found and Homebrew is unavailable." \
            "Install Homebrew from https://brew.sh, then re-run this script." >&2
        exit 1
    fi
    printf '%s' "Python 3.12+ is missing. Install it with 'brew install python@3.12'? [y/N] "
    ANSWER=
    if [ -r /dev/tty ]; then
        read -r ANSWER </dev/tty || ANSWER=
    fi
    case $ANSWER in
        y|Y|yes|YES) ;;
        *)
            printf '%s\n' "install-mac.sh: Python 3.12+ is required." >&2
            exit 1
            ;;
    esac
    brew install python@3.12
    BREW_PREFIX=$(brew --prefix python@3.12)
    PYTHON=$BREW_PREFIX/bin/python3.12
    if ! python_ok "$PYTHON"; then
        printf '%s\n' "install-mac.sh: Python 3.12+ still not available after brew." >&2
        exit 1
    fi
fi

ARGS="$*"
MODE=--install
LAUNCH=1
case " $ARGS " in
    *" --install "*) MODE= ;;
    *" --check "*|*" --uninstall "*) MODE=; LAUNCH= ;;
esac

"$PYTHON" "$REPO_DIR/scripts/install.py" --platform macos $MODE "$@"
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
    exit "$STATUS"
fi

if [ "$LAUNCH" -eq 1 ]; then
    PLAYER=$HOME/.local/bin/dvd-player
    if [ -x "$PLAYER" ]; then
        exec "$PLAYER"
    fi
    printf '%s\n' \
        "install-mac.sh: install finished, but $PLAYER was not found." \
        "Run: uv tool install --force \"$REPO_DIR\"" >&2
    exit 1
fi
exit 0
