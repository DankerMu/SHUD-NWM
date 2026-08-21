# Tasks

## 1. Implementation (implementer)

- [x] 1.1 `tests/fixtures/legacy_qhh_fallback_pre_1413.sql`: frozen pre-#1413 statement (design D1), header comment with provenance + never-update rule; `_LEGACY_FALLBACK_SQL` constant in `tests/test_display_coverage_residual_debt_integration.py` reading it.
- [x] 1.2 `_seed_station_coverage` private helper (design D3: 1 station, 6 interp weights, 12 forcing rows — NOT in `seed_issue_126_data`), `_NEW_ONLY_COLUMNS` + `_parity_pair` with asserted projection (design D2), and tests: (i) covered state with station non-vacuity, (ii) NULL-identity candidate (autocommit insert, `run_manifest_uri` supplied; station rows present and excluded on both sides), (iii) empty header → both empty.
- [x] 1.3 Negative control test (design D3 iv): `'1 hour'` → `'1 second'` mutant via monkeypatch, forced fallback still active → projected parity must fail.
- [x] 1.4 Module docstring: add the parity scope bullet; keep the three existing tests unchanged.

## 2. Spec

- [x] 2.1 Spec delta: MODIFIED "Fallback candidate query scan discipline" — "Result parity" scenario names the frozen pre-pushdown statement as baseline, reproducible in-repo; `openspec validate qhh-fallback-parity-frozen-oracle --strict --no-interactive`.

## Evidence Floor

- [x] E1 Local: `uv run ruff check .`; `uv run pytest -q tests/test_display_coverage_residual_debt_integration.py --collect-only` (7 collected, clean skip without DSN); `uv run pytest -q tests/test_qhh_latest_fallback_pushdown.py tests/test_river_ts_text_identity_cleanup.py` green (census untouched).
- [x] E2 node-27 throwaway DB (superuser DSN from `node27-timeseries-compression.env`, `NHMS_RUN_INTEGRATION=1`): `uv run pytest -q tests/test_display_coverage_residual_debt_integration.py` → 7 passed; receipt on the PR.
- [x] E2b node-27 mutant receipt (AC "反向验证"): in a throwaway worktree, `sed` one scan binding (`scan_display_end=header["display_end_time"]` → `header["display_start_time"]`) in `packages/common/forecast_store.py`, re-run the parity tests → (i) red, then revert. Receipt on the PR.
- [x] E3 CI green on the final head.
- [x] E4 Routing comment on #1342 naming the frozen fixture as a consumer to re-freeze/retire (design D5).
