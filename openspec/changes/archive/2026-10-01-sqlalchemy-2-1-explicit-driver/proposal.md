# sqlalchemy-2-1-explicit-driver

## Why

#2632: PR #2631 capped `pyproject.toml` at `sqlalchemy>=2.0.25,<2.1`. SQLAlchemy 2.1.0 changed the default DBAPI for plain `postgresql://` URLs from psycopg2 to psycopg (v3), but the project only installs `psycopg2-binary`. CI installs dependencies with `pip install -e ".[dev]"` rather than from `uv.lock`, so it picked up 2.1 straight away: 51 tests failed with `ModuleNotFoundError: No module named 'psycopg'` (PR #2629). The cap freezes the project on the 2.0 line.

## What Changes

- **Driver choice:** keep psycopg2 and spell it out, rather than adding psycopg v3. All the raw-DBAPI code (`psycopg2.connect`, `RealDictCursor`, `psycopg2.errors`, the `copy_expert` paths) already runs on psycopg2. A second driver would bring different type adaptation and parameter behaviour into the same process for no gain.
- **One helper normalises SQLAlchemy URLs:** `packages/common/sqlalchemy_url.py::sqlalchemy_url(url)`.
  - `postgresql://` becomes `postgresql+psycopg2://`. `postgres://` is left alone and keeps failing as it does today.
  - URLs that already name a driver, and non-PostgreSQL URLs such as `sqlite://`, are returned unchanged.
  - The rewrite goes through `sqlalchemy.engine.make_url`, so credentials and query strings are preserved.
- **Every `create_engine` call routes through the helper:** production code and scripts, plus `tests/integration_helpers.py::sqlalchemy_engine` and any test that builds a PostgreSQL engine.
- **An AST guard test:** every `create_engine(...)` call in `apps/ packages/ services/ workers/ scripts/` and in `tests/integration_helpers.py` must receive `sqlalchemy_url(...)` as its first argument, or a literal `sqlite` URL.
- **Lift the cap:** `sqlalchemy>=2.0.25` with no upper bound. `uv lock` resolves to the latest 2.1.x. Any other 2.1 breaking changes the full suite surfaces are fixed in this change.

## Impact

- Files: `pyproject.toml`, `uv.lock`, the helper plus its tests, the engine factories, and `tests/integration_helpers.py`.
- Environment URLs such as `DATABASE_URL=postgresql://…` in `infra/env` stay as they are; normalisation happens at engine creation.
- node-22 is not touched (see its maintenance-window rule). On node-27 the upgrade is validated in a worktree with its own `.venv`. The active `/home/nwm/NWM/.venv`, which serves the display API, is not upgraded by this change. It picks up 2.1 only on a later, separate deploy. See the risks below.
