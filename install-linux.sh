#!/usr/bin/env sh
# One-line installer for Linux. Installs Python 3.12+ if missing, then
# delegates to scripts/install.py (which installs uv/mpv unattended).
# Wrong OS is refused with exit code 2; a failed bootstrap exits 1.
# Pass --ask to be prompted before each missing dependency is installed.
set -eu

# Resolve the physical script path through symlinks (POSIX: macOS readlink has
# no -f). Works from any cwd and with spaces/unicode in the path.
SCRIPT=$0
HOPS=0
while [ -h "$SCRIPT" ]; do
    HOPS=$((HOPS + 1))
    if [ "$HOPS" -ge 40 ]; then
        printf '%s\n' \
            "install-linux.sh: too many symbolic links resolving $0" \
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
    Linux*) ;;
    *)
        printf '%s\n' \
            "install-linux.sh: this installer is for Linux, but uname reports '$OS_NAME'." \
            "Use ./install-mac.sh on macOS or install-windows.cmd on Windows." >&2
        exit 2
        ;;
esac

# Missing Python 3.12+ is installed unattended by default; --ask opts out.
ASK=0
case " $* " in
    *" --ask "*) ASK=1 ;;
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

install_python() {
    # Word splitting is intended: PY_INSTALL_CMD holds a static command line.
    # shellcheck disable=SC2086
    set -- $PY_INSTALL_CMD
    if [ "$(id -u)" -eq 0 ] || ! command -v sudo >/dev/null 2>&1; then
        "$@"
    else
        sudo "$@"
    fi
}

PYTHON=$(find_python) || true
if [ -z "${PYTHON:-}" ]; then
    PY_INSTALL_CMD=
    if command -v apt-get >/dev/null 2>&1; then
        PY_INSTALL_CMD="apt-get install -y python3"
    elif command -v dnf >/dev/null 2>&1; then
        PY_INSTALL_CMD="dnf install -y python3"
    elif command -v pacman >/dev/null 2>&1; then
        PY_INSTALL_CMD="pacman -S --needed --noconfirm python"
    elif command -v zypper >/dev/null 2>&1; then
        PY_INSTALL_CMD="zypper --non-interactive install python3"
    elif command -v apk >/dev/null 2>&1; then
        PY_INSTALL_CMD="apk add python3"
    else
        printf '%s\n' \
            "install-linux.sh: Python 3.12+ not found and no supported package manager is available." \
            "Install Python 3.12+ manually, then re-run this script." >&2
        exit 1
    fi
    if [ "$ASK" -eq 1 ]; then
        printf '%s' "Python 3.12+ is missing. Install it with '$PY_INSTALL_CMD'? [y/N] "
        ANSWER=
        if [ -r /dev/tty ]; then
            read -r ANSWER </dev/tty || ANSWER=
        fi
        case $ANSWER in
            y|Y|yes|YES) ;;
            *)
                printf '%s\n' "install-linux.sh: Python 3.12+ is required." >&2
                exit 1
                ;;
        esac
    else
        printf '%s\n' "Python 3.12+ is missing; installing it (pass --ask to prompt)."
    fi
    install_python
    PYTHON=$(find_python) || true
    if [ -z "${PYTHON:-}" ]; then
        printf '%s\n' "install-linux.sh: Python 3.12+ still not available after install." >&2
        exit 1
    fi
fi

exec "$PYTHON" "$REPO_DIR/scripts/install.py" --platform linux "$@"
