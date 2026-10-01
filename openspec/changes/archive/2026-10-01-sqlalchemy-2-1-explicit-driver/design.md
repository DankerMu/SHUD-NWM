# Design — sqlalchemy-2-1-explicit-driver

## D1 Helper
```python
def sqlalchemy_url(url: str | URL) -> URL:
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        return parsed.set(drivername="postgresql+psycopg2")
    return parsed
```
- Returning a `URL` object means the password is never re-serialised into a string. `create_engine` accepts `URL` objects.
- `postgres://` is deliberately NOT rewritten. SQLAlchemy rejects it today (`NoSuchModuleError`), and it keeps rejecting it, so bad-URL error semantics are preserved.
- Unit tests cover:
  - a plain `postgresql://` URL;
  - `postgres://` left unchanged, so it still fails as it does today;
  - an explicit `+psycopg2` URL left unchanged;
  - an explicit `+psycopg` (v3) URL left unchanged, because the caller chose it;
  - a sqlite URL left unchanged;
  - a password with special characters and a query string such as `?options=-c%20…`, both preserved;
  - the engine's `dialect.driver == "psycopg2"` for a plain URL, checked with `create_engine(..., strategy-free)`: engines connect lazily, so no DB is needed.

## D2 Call sites
Route every production `create_engine` call through the helper:
- `apps/api/routes/pipeline.py`
- `apps/api/routes/hydro_display.py`
- `services/tile_publisher/forcing_copyback_backfill.py`
- `services/tile_publisher/publisher.py`
- `services/orchestrator/chain_compat_runtime.py`
- `services/orchestrator/scheduler_runtime.py` (keep its make_url error semantics: a bad URL must still raise synchronously where it does today)
- `scripts/node27_coverage_freshness_alert.py`
- `scripts/node27_river_tile_coordinate_evidence.py`
- any other site found by grep, including `sqlalchemy.create_engine` attribute calls

Also route `tests/integration_helpers.py::sqlalchemy_engine`.

**Call inside the factory body.** Call the helper inside each factory body, for example the `_engine()` bodies at `apps/api/routes/hydro_display.py:145` and `apps/api/routes/pipeline.py:139`. Their `lru_cache` keys must stay the original DSN string.

**Tests that assert the exact URL.** `URL.__eq__` requires another `URL`, so these assertions change:
- `tests/test_node27_connection_attribution.py:277` (`captured["url"] == DSN`)
- `tests/test_node27_connection_attribution.py:312` (`calls == [DSN, DSN_WITH_OVERRIDE]`)
- `tests/test_node27_connection_attribution.py:1530` (the pipeline engine)
- `tests/test_node27_coverage_freshness_alert.py:867` (the fake `create_engine`)

Each assertion is rewritten to compare against `sqlalchemy_url(DSN)` or `str(...)` of it. No assertion may be deleted. The fakes must accept a `URL` object.

## D3 Guard test
`tests/test_sqlalchemy_driver_explicit.py` works as follows:
- Parse every `.py` file under `apps/`, `packages/`, `services/`, `workers/` and `scripts/`, plus `tests/integration_helpers.py`.
- Find each call whose callee is `create_engine` or `*.create_engine`.
- Assert that its first positional argument is a call to `sqlalchemy_url`, or a string/f-string literal starting with `sqlite`.

The test also includes a mutation self-check: a synthetic source string with a bare `create_engine(database_url)` must be flagged. Route the new test and helper through `scripts/select_ci_tests.py`; the selector meta-tests must stay green.

## D4 Dependency
- `pyproject.toml`: `"sqlalchemy>=2.0.25"`. The explicit driver makes 2.0 and 2.1 behave the same.
- `uv lock --upgrade-package sqlalchemy` resolves to the latest 2.1.x. No other package is upgraded. `greenlet` drops out of the lock, because 2.1 only pulls it in with the `asyncio` extra and nothing here uses `sqlalchemy.ext.asyncio`; record the removal.
- The locked version is recorded.

## D5 Other 2.1 changes
- Run the full local suite on 2.1, then the full node-27 suite (see Evidence).
- Fix any genuine 2.1 incompatibility here, with a before/after note. Do not add retries or skips to hide one.

## Risks
- **Deployment gap on node-27.** The active node-27 venv stays on 2.0.49 until it is synced. The systemd units run `.venv/bin/python` directly, and no timer syncs implicitly.
  - However, any bare `uv run` in `/home/nwm/NWM` after the new lock is pulled upgrades the active venv to 2.1, beneath the running display API. The standard development loop does exactly this: `git pull` followed by `uv run pytest`.
  - The helper is validated on 2.0.49 (task 2.0) and on 2.1, so either version is correct for this code. Still, the upgrade should be a deliberate deploy step: `uv sync` followed by a display API restart via `scripts/ops/start-display-api.sh`, with a receipt.
  - The PR records this. After merge, and only once the owner confirms, the orchestrator performs that step on node-27, restarting only `nhms-display-api.service` and leaving yd-web alone. Until then, node-27 operators use `uv run --no-sync`.
- **Environments that build engines from URLs outside the helper,** for example ad-hoc scripts not under `scripts/`. These are outside the guard and are listed in the PR.
