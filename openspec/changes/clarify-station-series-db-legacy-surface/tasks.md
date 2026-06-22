## 1. Fixture And Decision

- [x] 1.1 Add OpenSpec fixture for #630.
- [x] 1.2 Decide helper status: keep as legacy/internal DB surface, not public display route.
- [x] 1.3 Validate fixture with `openspec validate clarify-station-series-db-legacy-surface --strict --no-interactive`.

## 2. Code, Tests, Docs

- [x] 2.1 Mark `PsycopgForecastStore.station_series()` as legacy/internal in code documentation.
- [x] 2.2 Add static regression coverage that production code does not call the legacy DB helper.
- [x] 2.3 Update object-store station-series runbook with the legacy helper boundary.

## 3. Verification

- [x] 3.1 `uv run pytest -q tests/test_forecast_api_met_station_series.py tests/test_forecast_api.py` PASS, 151 passed.
- [x] 3.2 `uv run ruff check .` PASS.
- [x] 3.3 `openspec validate clarify-station-series-db-legacy-surface --strict --no-interactive` PASS.
- [x] 3.4 `openspec validate --all --strict --no-interactive` PASS, 177 passed.
