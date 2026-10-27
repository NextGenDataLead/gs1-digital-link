# Contributing

Thanks for your interest in the GS1 Digital Link Orchestrator.

## Before you start

Read the two authoritative documents, in order:

1. [docs/PROJECT_HANDOVER.md](docs/PROJECT_HANDOVER.md) — the *why*.
2. [docs/IMPLEMENTATION_SPEC.md](docs/IMPLEMENTATION_SPEC.md) — the *how*. This is
   the operational source of truth for conventions, module contracts, and the
   Definition of Done per phase.

If the spec does not cover something, open an issue and ask rather than
inventing a convention.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows: `uv sync --python 3.11 --extra dev --extra ui --locked`, then `.venv\Scripts\activate`.
See [`docs/setup.md`](docs/setup.md#1-install). CI runs the tests on Linux and Windows, and ruff and mypy on Linux.

## Conventions

Per `IMPLEMENTATION_SPEC.md` §1:

- **Python 3.11+**, PEP 604 union syntax (`str | None`).
- **Type hints mandatory**; `mypy --strict lib` must pass.
- **Google-style docstrings** on every public function and class.
- **Formatter:** `ruff format`. **Linter:** `ruff check` (rules `E,F,I,N,UP,B,SIM,PL`).
- **Line length:** 100. Absolute imports only. `httpx` for HTTP, stdlib `json`.
- Raise typed exceptions from `lib.errors`; never bare `raise Exception(...)`.
- Use `logging`, never `print()`. Never log secrets.

## Checks before opening a pull request

All four must pass locally and in CI:

```bash
ruff check
ruff format --check
mypy --strict lib
pytest
```

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
`fix:`, `refactor:`, `docs:`, `test:`, `chore:`, `perf:`, `ci:`. Keep commits
small and logically scoped.

## Secrets

Never commit `clients.yml`, `.env`, or any real credential. Secrets live in
environment variables referenced by name from `clients.yml`.
