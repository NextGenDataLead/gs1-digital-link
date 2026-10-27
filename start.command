#!/bin/bash
#
# Open the GS1 Digital Link operator shell.  Double-click me.
#
# Opens a desktop window bound to 127.0.0.1 — there is no shareable URL and no browser tab.
# `./start.command --browser` serves the same pages in a browser instead, for a machine with
# no webview available.
#
# Run install.command first, once.

set -euo pipefail

# Must match install.command; tests/test_packaging.py checks that it does.
PYTHON_VERSION="3.11"

# uv finds the project from here.  The data is elsewhere: see GS1_DATA_DIR below.
cd -- "$(dirname -- "$0")"

pause() {
    if [ -t 0 ]; then
        printf '\nPress return to close this window. '
        read -r _
    fi
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

if ! UV="$(find_uv)"; then
    echo "This machine has not been set up yet — double-click install.command first."
    pause
    exit 1
fi

# The data folder the installer recorded (or one IT already pinned in GS1_DATA_DIR).  Without
# it the shell would fall back to this folder, which on a release install holds no data.
if ! GS1_DATA_DIR="$("$UV" run --frozen --extra ui --python "$PYTHON_VERSION" python -m scripts.data_folder)"; then
    echo "Double-click install.command first — it sets up the data folder."
    pause
    exit 1
fi
export GS1_DATA_DIR
echo "Data folder: $GS1_DATA_DIR"

# --frozen: use uv.lock exactly as committed and never update it.  Starting the app is not the
# moment to resolve new versions of anything that talks to a live site.
if ! "$UV" run --frozen --extra ui --python "$PYTHON_VERSION" python -m ui "$@"; then
    echo
    echo "── The operator shell exited with an error. ─────────────────────────────────"
    echo "If it never opened a window, run install.command again first."
    echo "Otherwise send the lines above to whoever maintains this tool."
    pause
    exit 1
fi
