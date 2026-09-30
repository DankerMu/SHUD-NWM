# Tasks — sqlalchemy-2-1-explicit-driver

Fixture level: standard. Risk packs: dependency-upgrade, runtime-config (DB driver).

## Must preserve
- Every PostgreSQL engine uses psycopg2, as it does today. Non-PostgreSQL URLs are unchanged.
- The error semantics of engine factories on a bad URL, such as `scheduler_runtime`'s synchronous raise, are unchanged.
- node-22 is not touched. On node-27 the active `.venv` is not synced by the validation run; this is proven in the receipt. The post-merge deploy (sync plus display API restart) is a separate, receipted step.
- `postgres://` keeps failing as it does today.

## 1. Helper and call sites
- [x] 1.1 Add `packages/common/sqlalchemy_url.py` and its unit tests (D1).
  - Tests: `tests/test_sqlalchemy_url.py` (same-name route in the selector).
- [x] 1.2 Route every `create_engine` call site through the helper, including `tests/integration_helpers.py` (D2).
  - Nine sites: the eight D2 files plus `tests/integration_helpers.py`. Every `create_engine` under `tests/` besides that helper is a literal `sqlite://` engine.
- [x] 1.3 Add the AST guard test with a mutation self-check, and route it in the selector (D3).
  - `tests/test_sqlalchemy_driver_explicit.py`. On the pre-change tree it names all nine sites; bypassing the helper at `services/tile_publisher/publisher.py` alone reddens it naming that line.
  - Selector: a changed `.py` under the guard roots that calls `create_engine`, or the helper itself, adds the guard (#2498 sniff shape); `pyproject.toml` / `uv.lock` add the helper suite. `tests/integration_helpers.py` keeps its #1487 carve-out collapse (guard runs on master only for that file).

## 2. Dependency
- [x] 2.0 Before touching the lock, still on SQLAlchemy 2.0.49: the helper unit tests and the four exact-URL tests (D2) pass. Record the output.
  - SQLAlchemy 2.0.49: `tests/test_sqlalchemy_url.py tests/test_sqlalchemy_driver_explicit.py tests/test_gateway_reconcile_store_reset.py tests/test_node27_coverage_freshness_alert.py tests/test_node27_connection_attribution.py` → `190 passed`; with `tests/test_node27_connection_attribution_delegated.py` added → `217 passed`.
- [x] 2.1 Lift the cap in `pyproject.toml`. Run `uv lock --upgrade-package sqlalchemy` and record the resolved 2.1.x. No other package is upgraded; `greenlet` is removed, and the removal is recorded.
  - `uv lock`: `Removed greenlet v3.5.0`, `Updated sqlalchemy v2.0.49 -> v2.1.1`; no other `name`/`version` line in `uv.lock` changed.
- [x] 2.2 Show red then green: on 2.1 without the helper, a plain-URL engine imports psycopg (v3) and fails; with the helper it uses psycopg2.
  - SQLAlchemy 2.1.1: plain `postgresql://` → default driver `psycopg`, `create_engine` raises `ModuleNotFoundError: No module named 'psycopg'`; `create_engine(sqlalchemy_url(url))` → `postgresql+psycopg2`, `dialect.driver == 'psycopg2'`.

## 3. Verification
- [x] 3.1 Local: `uv sync --all-extras --dev`, `uv run ruff check .`, and the full `uv run pytest -q` on 2.1.x.
  - macOS, SQLAlchemy 2.1.1: ruff clean. Full suite `12 failed, 21483 passed, 403 skipped`. Two were selector exact-set pins for `scheduler_runtime.py` gaining the new guard rider (updated; `tests/test_select_ci_tests.py` then `949 passed`). The other ten (setgid-mode `test_tile_publisher.py` / `test_canonical_precip_copyback_backfill.py`, device-label `test_node27_working_set.py`) fail identically on the base commit with SQLAlchemy 2.0.49: pre-existing macOS-only. No SQLAlchemy 2.1 incompatibility surfaced.
- [ ] 3.2 node-27 full pytest in an isolated worktree venv:
  - **Get the code.** `git fetch origin <branch>`, then `git worktree add --detach $WT FETCH_HEAD`. Never pull the shared checkout.
  - **Isolate uv.** Every uv call runs as `env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT="$WT/.venv" uv ...`, with `TMPDIR=/home/nwm/tmp`.
  - **Build and run.** Run `uv sync --all-extras --dev`, then `uv run pytest -q` over the whole suite. Run it twice:
    - with the default gates;
    - with `NHMS_RUN_INTEGRATION=1` and a throwaway-DB DSN, as `-m integration`.
  - **Receipt.**
    - The worktree's `sys.prefix` and SQLAlchemy version: the prefix must be under `$WT`, and the version must be 2.1.x.
    - `/home/nwm/NWM/.venv/bin/python -c 'import sqlalchemy;print(sqlalchemy.__version__)'` before and after the run: both must print 2.0.49.
    - `git worktree remove` at the end.
- [ ] 3.3 CI: SQL Migration Dry Run (the pip-install path) is green on the PR.

## 4. Post-merge deploy (after the owner confirms; not part of this PR)
- [ ] 4.1 On node-27, in `/home/nwm/NWM`: run `git pull --ff-only`, then `uv sync --all-extras --dev`, then restart the display API with `scripts/ops/start-display-api.sh`, leaving yd-web alone. Check `/api/v1/basins` returns 200 and that the active venv now reports SQLAlchemy 2.1.x. Write a receipt.

## Evidence Floor
- The `<2.1` cap is removed. `uv.lock` pins 2.1.x.
- The full node-27 pytest passes on 2.1.x in the worktree venv, with the receipt recorded.
- CI SQL Migration Dry Run is green.
- The guard test turns red on a bare `create_engine(postgres_url)`.
