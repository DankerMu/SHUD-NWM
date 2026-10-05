# Tasks

## 1. Implementation

- [x] 1.1 `_upsert_met_stations`: an existing station row that satisfies the identity predicate gets no new
      tuple version; a missing one is inserted; an incompatible one raises `HANDOFF_APPLY_STATION_CONFLICT`.
      The SQL predicate terms are unchanged.
- [x] 1.2 `tests/test_forcing_domain_handoff_apply.py`: the fake connection follows the new statement; all
      existing tests keep their assertions.
- [x] 1.3 Real-PostgreSQL tests (file-level `pytestmark = pytest.mark.integration`, never `timescaledb_210`) for
      the six cases in the proposal's evidence list.
- [x] 1.4 `.github/workflows/ci.yml`: add `packages/common/forcing_domain_handoff_apply.py` and the new suite to
      the `database` filter; register the (module, suite) pair the way
      `tests/test_select_ci_tests.py::test_river_expand_sources_open_the_database_lane` does, and add the
      module to `INTEGRATION_TRIGGER_SOURCES` so removing the filter entry turns a test red.
- [x] 1.5 `evidence/2026-10-05-node27-met-station-probe.txt`: the raw node-27 read-only probe output.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; `uv run pytest -q tests/test_forcing_domain_handoff_apply.py
      tests/test_direct_grid_variant_registration.py` and the selector / CI-contract tests touched by 1.4.
- [ ] 2.2 CI: `SQL Migration Dry Run` runs and the new integration tests pass there (not skipped). The PR is
      non-draft at the last push.
- [x] 2.3 Red-before: the "second apply leaves ctid/xmin unchanged" test fails on the unmodified function.
- [ ] 2.4 node-27, after merge: pull at a tick boundary; `n_tup_upd` delta on `met.met_station` across one
      full ingest tick compared with the pre-deploy figure (about 830 per minute during ingest; 268,328 in 31 h).
      No scratch database and no migration on node-27.
