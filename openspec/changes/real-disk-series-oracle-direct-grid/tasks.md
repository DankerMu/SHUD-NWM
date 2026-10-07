# Tasks

## 1. Implementation

- [x] 1.1 Support module: per-cycle combo resolution (dg directory, station from the resolver, settled file),
      newest complete cycle, diagnostic; unit suite cases.
- [x] 1.2 Real-disk suite on resolved combos; the legacy-model 404 negative; printed selection.
- [x] 1.3 Direct Grid shape baseline fixture (compact, one line); the legacy fixture stays untouched.
- [x] 1.4 CI selector: unchanged rules still select the support suite for the touched files (extend only if
      a new file needs it).

## 2. Evidence Floor

- [ ] 2.1 Local: `uv run ruff check .`; `uv run pytest -q tests/test_object_store_forcing_real_disk_support.py
      tests/test_object_store_forcing_real_disk.py` (the second skips locally); `tests/test_select_ci_tests.py`
      after staging; `openspec validate real-disk-series-oracle-direct-grid --strict --no-interactive`.
- [ ] 2.2 CI green on the PR.
- [ ] 2.3 node-27 after merge, read-only as `nhms_display_ro`:
      `NHMS_RUN_E2E=1 NHMS_RUN_REAL_DISK=1 uv run --no-sync pytest tests/test_object_store_forcing_real_disk.py -rA -s`
      passes; the output with the four chosen combos is the receipt (also closes #2595).

Deviation: the suite cannot run locally or in CI (no real store); its only real evidence is 2.3. node-27 run
is read-only (files and SELECTs), within the allowed node-27 operations; no test database, no migration.
