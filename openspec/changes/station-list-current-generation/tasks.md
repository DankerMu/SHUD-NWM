# Tasks

## 1. Implementation

- [x] 1.1 `list_met_stations` basin-only branch: latest-displayable-run membership predicate; comment why.
- [x] 1.2 Contract pins in `tests/test_list_search_contract.py` (incl. the shifted #1669 positional pins).
- [x] 1.3 Real-database result cases and the basin-only plan-shape case in
      `tests/test_met_station_model_filter_integration.py`.
- [x] 1.4 Comment in `apps/frontend/src/stores/stationLayerData.ts` (~:85-88): the basin list now needs a
      displayable run. Comment only.

## 2. Evidence Floor

- [ ] 2.1 Local: `uv run ruff check .`; `uv run pytest -q tests/test_list_search_contract.py
      tests/test_forecast_api.py tests/test_api_contract_resources.py`; `tests/test_select_ci_tests.py` after
      staging; `openspec validate station-list-current-generation --strict --no-interactive`.
- [ ] 2.2 CI green on the PR, including the real-database lane that runs
      `tests/test_met_station_model_filter_integration.py`.
- [ ] 2.3 node-27 after merge and display redeploy, read-only: `total_count` for heihe (287), qhh (71), one
      basin listing 0 today, one retired basin (0).

Deviation: node-27 real-DB pytest receipt is deferred until the RAID link is repaired (no test database may
be created there); pending command: `uv run pytest -q tests/test_met_station_model_filter_integration.py` on
node-27. Local results are not a node-27 PASS.
