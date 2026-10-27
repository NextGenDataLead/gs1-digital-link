#!/bin/sh
#
# First start against an empty data folder: give it the two files the Setup screen edits, from
# the examples that ship with this image. Never overwrites — an existing clients.yml or .env is
# the installation's own, and the ledger beside it is permanent.
set -eu

data="${GS1_DATA_DIR:-/data}"
code="${GS1_CODE_DIR:-/app}"   # overridable so tests/test_container.py can run this for real

if [ ! -w "$data" ]; then
    echo "The data folder $data is not writable by this container (uid $(id -u))." >&2
    echo "On Linux, start it with the folder owner's uid: see docs/operator-install.md." >&2
    exit 1
fi

mkdir -p "$data/input" "$data/output"

if [ ! -e "$data/clients.yml" ]; then
    cp "$code/clients.example.yml" "$data/clients.yml"
    echo "New data folder: wrote clients.yml from the example. Fill it in on the Setup screen."
fi

if [ ! -e "$data/.env" ]; then
    (umask 077 && cp "$code/.env.example" "$data/.env")
    echo "New data folder: wrote .env from the example (owner-only). Credentials go there."
fi

exec "$@"
