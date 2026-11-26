#!/usr/bin/env sh
# One-line installer for macOS. Delegates to scripts/install.py.
# Wrong OS is refused with exit code 2; missing Python exits 1.
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

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' \
        "install-mac.sh: python3 not found." \
        "Install it with: brew install python@3.12" >&2
    exit 1
fi

exec python3 "$REPO_DIR/scripts/install.py" --platform macos "$@"
