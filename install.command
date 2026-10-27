#!/bin/bash
#
# Install the GS1 Digital Link operator shell on this machine.  Double-click me.
#
# There is nothing to install first.  `uv` is a single static binary that fetches its own
# CPython, so this works on a machine with no Python, no Homebrew and no developer tools.
# The packages come from the committed uv.lock, so this machine gets the versions that were
# tested rather than whatever resolves today.
#
# Safe to run again at any time — it is idempotent.  To update, download the new release, unzip it
# anywhere, and run its installer: it finds this machine's data folder and leaves it as it is.
# It writes inside this folder, inside ~/.local, and — on a first install only — a data folder in
# your home folder plus a one-line note of where it is (lib/install_location.py has the rules).
#
# Maintainers: running this in a development clone REPLACES .venv with a Python 3.11
# environment holding the `ui` extra but not `dev`.  Use `pip install -e ".[dev,ui]"` there.

set -euo pipefail

# Pinned on purpose, and checked by tests/test_packaging.py:
#   UV_VERSION     — the uv that produced the committed uv.lock.  A newer uv reads it fine; an
#                    older one may not know its revision.  Bump it here, in install.bat and in
#                    .github/workflows/ci.yml together, and re-run `uv lock`.
#   PYTHON_VERSION — matches the Python that CI runs the test suite on.  Pinned in the two
#                    installers rather than in a .python-version file, because that file is
#                    also read by pyenv, where it would break `python` in this directory for
#                    anyone who has pyenv but not 3.11.
UV_VERSION="0.11.6"
PYTHON_VERSION="3.11"

cd -- "$(dirname -- "$0")"

pause() {
    if [ -t 0 ]; then
        printf '\nPress return to close this window. '
        read -r _
    fi
}

fail() {
    echo
    echo "── Installation failed (line ${1}). ─────────────────────────────────────────"
    echo "Nothing outside this folder was changed, and nothing was published."
    echo "Send the lines above to whoever maintains this tool.  The two failures that"
    echo "have a known fix — a blocked download, and macOS refusing to open this file —"
    echo "are written up in docs/operator-install.md."
    pause
    exit 1
}
trap 'fail $LINENO' ERR

if [ ! -f pyproject.toml ]; then
    echo "This script has to stay in the project folder: it installs the tool that sits"
    echo "beside it, and pyproject.toml is not here.  Move it back and try again."
    pause
    exit 1
fi

# `command -v` first so an existing uv — Homebrew's, or one IT deployed — is used as-is
# rather than a second copy being installed beside it.
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

if UV="$(find_uv)"; then
    echo "Using uv already on this machine: $UV"
else
    echo "Installing uv ${UV_VERSION} into ~/.local/bin …"
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | UV_INSTALL_DIR="$HOME/.local/bin" sh
    if ! UV="$(find_uv)"; then
        echo "uv installed but could not be found afterwards.  Look in ~/.local/bin."
        pause
        exit 1
    fi
fi

echo
echo "Fetching Python ${PYTHON_VERSION} …"
"$UV" python install "$PYTHON_VERSION"

echo
echo "Installing the tool and its packages from uv.lock …"
# --locked refuses to resolve anything new: if uv.lock no longer matches pyproject.toml the
# install stops here rather than quietly giving this machine a different set of versions.
"$UV" sync --extra ui --locked --python "$PYTHON_VERSION"

echo
echo "Checking the operator shell can start …"
"$UV" run --frozen --extra ui --python "$PYTHON_VERSION" python -c "import ui.app"

echo
echo "Finding this machine's data folder …"
# Settings, credentials and the record of what was published live there, not in this folder,
# so a newer release can replace this one without touching them.  It refuses — and stops the
# install — rather than start a second, empty one beside data it cannot account for.
"$UV" run --frozen --extra ui --python "$PYTHON_VERSION" python -m scripts.data_folder --install >/dev/null

echo
echo "── Done. ────────────────────────────────────────────────────────────────────"
echo "Double-click start.command to open the operator shell."
echo
echo "On a new installation, fill in the site settings and credentials on the Setup"
echo "screen — or ask whoever set this up for the clients.yml and .env that go in the"
echo "data folder named above.  They are never part of the download."
pause
