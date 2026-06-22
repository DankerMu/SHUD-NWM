## 1. Fixture And Contract

- [x] 1.1 Add OpenSpec delta requiring chunked, bounded station CSV line reads.
- [x] 1.2 Review the fixture for selected file IO/resource/error risk packs.
- [x] 1.3 Validate the change with `openspec validate optimize-object-store-csv-chunked-reader --strict --no-interactive`.

## 2. Reader Regression

- [x] 2.1 Add a regression test that forces tiny read chunks and proves a valid CSV split across chunks still parses correctly.
- [x] 2.2 Keep existing oversized file, overlong line, row-count, no-follow, and route tests passing without weakening assertions.
- [x] 2.3 Preserve public response shape, variables, timestamp, and stable error-code behavior.

## 3. Verification

- [x] 3.1 `uv run pytest -q tests/test_object_store_forcing.py tests/test_forecast_api_met_station_series.py` PASS.
- [x] 3.2 `uv run ruff check .` PASS.
- [x] 3.3 `openspec validate optimize-object-store-csv-chunked-reader --strict --no-interactive` PASS.
