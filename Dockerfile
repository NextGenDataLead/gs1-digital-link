# syntax=docker/dockerfile:1
#
# The operator shell as a container image — the install route for machines where IT runs
# containers but will not allow `uv` to fetch a Python. The double-click installers remain the
# default; see docs/operator-install.md.
#
# The image holds code only. Everything an installation owns — .env, clients.yml, input/,
# output/ and the state.json ledger in it — lives in the volume mounted at /data, so pulling a
# newer image can never replace the ledger. .dockerignore keeps every one of those out of the
# build context, and tests/test_container.py holds both rules in place.
#
# Pinned, and checked against install.command / install.bat / ci.yml by tests/test_packaging.py:
#   uv 0.11.6 (the uv that wrote uv.lock) and Python 3.11 (what CI runs the suite on).

FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM python:3.11-slim

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GS1_DATA_DIR=/data

WORKDIR /app

# Dependencies first, from the lockfile, so a code-only change rebuilds one thin layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --extra ui --no-install-project

# Code and the assets that ship with it. The data folder's contents never come from here.
COPY lib/ lib/
COPY scripts/ scripts/
COPY ui/ ui/
COPY schema/ schema/
COPY reference/ reference/
COPY templates/ templates/
COPY prompts/ prompts/
COPY clients.example.yml .env.example ./
COPY docker/entrypoint.sh /usr/local/bin/gs1-entrypoint
# Editable, on purpose: the code finds schema/, prompts/ and templates/ beside lib/ (CODE_ROOT in
# lib/data_dir.py). Installed into site-packages, lib/ would look for them there and find nothing.
RUN uv sync --locked --extra ui \
    && chmod 0755 /usr/local/bin/gs1-entrypoint

# Not root. The data volume is the only writable place this process needs.
RUN useradd --create-home --uid 1000 gs1 && mkdir /data && chown gs1:gs1 /data
USER gs1
VOLUME ["/data"]

EXPOSE 8477
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8477/data', timeout=4)"
ENTRYPOINT ["gs1-entrypoint"]
CMD ["python", "-m", "ui", "--container"]
