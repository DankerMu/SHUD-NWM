# #1632 receipt — provider_atomic-closure marker suites under umask 0002 (node-27)

- Host: node-27 (`ghdc`), user `nwm`, 2026-09-23T13:24:53Z–13:31:15Z.
- Tree: isolated detached worktree `/home/nwm/tmp/wt-batchH-1632` at master `de4d1d182f64f4ff775129287505473315ec5e1a`, Python 3.11.15.
- DB: disposable scratch PostgreSQL from the `nhms-db` image (`sha256:ad39c4fb…`), `127.0.0.1:55560`. It is not `nhms-db`.
- Environment: explicit `umask 002` (log line `umask=0002`); `NHMS_RUN_INTEGRATION=1` + `NHMS_INTEGRATION_DATABASE_URL`, `NHMS_RUN_E2E=1`, `NHMS_RUN_GRIB=1`.
- Invocation: one `uv run pytest -q -rs -p no:cacheprovider <file>` per file.
- Script: `n27-umask-receipt.sh`. Log: `/home/nwm/tmp/oracle-batchH-1632/run-de4d1d182f64f4ff775129287505473315ec5e1a.log`.
- File set: the 21 marker-carrying files that `receipts/provider_atomic_closure.py` reports at `de4d1d182` (421 closure modules, 248 test files).
  - Pass 1 (13:24:53–13:31:15Z) ran 17 of them. That pass used an earlier resolver, which missed submodule edges.
  - Pass 2 (13:41:44–13:44:10Z) ran the other 4, in the same worktree and against a fresh scratch PG. Log: `/home/nwm/tmp/oracle-batchH-1632/run2-de4d1d182f64f4ff775129287505473315ec5e1a.log`.
  - #1513 counted 9 files and recorded no names, so this set cannot be diffed against it.

| File | Result under umask 0002 | Not measured (reason) |
|---|---|---|
| `test_basins_registry_import_db.py` | 4 passed | 1 skipped: `:302` real Basins import smoke is opt-in and needs `data/Basins` |
| `test_basins_registry_import_qhh.py` | 22 passed | — |
| `test_display_coverage_residual_debt_integration.py` | 8 passed | — |
| `test_e2e.py` | 4 passed | — |
| `test_e2e_ifs.py` | 2 passed, **2 failed** | Both failures are environmental. node-27 has no ecCodes library (`RuntimeError: Cannot find the ecCodes library`; xarray engine `cfgrib` does not load; `ldconfig -p` shows 0 ecCodes entries). They fail identically under `umask 022` in the same worktree. Failing tests: `test_ifs_adapter_canonical_forcing_run_parse_e2e` and `test_ifs_06z_144h_manifest_context_and_forcing_limit`. Neither is a provider-gate failure; both are **not measured for umask**. |
| `test_forecast_series_run_identity_pushdown_integration.py` | 2 passed | — |
| `test_hydro_run_parsed_at_integration.py` | 3 passed | — |
| `test_model_activation_audit_integration.py` | 10 passed | — |
| `test_orchestration_chain.py` | 490 passed | — |
| `test_pipeline_job_provenance_importer_integration.py` | 3 passed | — |
| `test_qhh_output_stream_type_integration.py` | 3 passed | — |
| `test_qhh_production_bootstrap_scheduler.py` | 11 passed | — |
| `test_real_basin_discovery_integration.py` | 3 passed | — |
| `test_real_database_integration.py` | 15 passed | — |
| `test_river_identity_normalization_integration.py` | 16 passed | — |
| `test_river_timeseries_stats_index_choice_integration.py` | 1 passed | — |
| `test_river_ts_dual_write_integration.py` | 17 passed | — |
| `test_mvt_national_identity_probe_integration.py` (pass 2) | 21 passed | — |
| `test_river_ts_read_path_surrogate_keys_integration.py` (pass 2) | 25 passed | — |
| `test_production_met_validation.py` (pass 2) | 32 passed, **6 failed** | Environmental, same cause as `test_e2e_ifs.py`: no ecCodes, so the `cfgrib` engine is unavailable. The same 6 fail under `umask 022`: `test_validate_met_default_lane_writes_required_evidence_and_redacts`, `test_validate_met_manifest_bound_counts_actual_deterministic_sources`, `test_validate_met_same_run_requires_force_and_force_replaces_bundle`, `test_validate_met_disabled_sources_record_skipped_without_success`, `test_validate_met_cached_only_policy_uses_cached_fixture`, `test_argparse_validate_met_fallback`. **Not measured for umask.** |
| `test_object_store_forcing_real_disk.py` (pass 2, run on its own, read-only) | **4 failed** | Stale fixture, not umask. The test hardcodes `LATEST_CYCLE = 2026-06-20T12:00:00Z`, and the node-27 object store now keeps `forcing/ifs/` from `2026080912` onward (89 cycles; raw retention removed June). Result: `404 STATION_FORCING_FILE_NOT_FOUND`, and the same 4 fail under `umask 022`. This invocation used `db_user=nhms_display_ro`, `NHMS_SERVICE_ROLE=display_readonly`, `statement_timeout=20000`, `lock_timeout=2000`, and set no integration URL. **Not measured for umask.** |

**Totals over 21 files: 692 passed, 12 failed, 1 skipped.**
- All 12 failures are environmental or stale-fixture, and each fails identically under `umask 022`: ecCodes missing (8), and a June cycle that retention has already removed (4).
- The 1 skip is the opt-in real-`data/Basins` import (`NHMS_RUN_REAL_BASINS_IMPORT` plus `data/Basins`). It was not enabled: a full basin import is out of this receipt's scope.

No file failed on `provider_lock_parent_unsafe` or `provider_destination_access_invalid`, so there is nothing to fix for #1632 under design D2.

Static census over all 21: `test_orchestration_chain.py` is the only one that names a publish/lock entry, and it names `copyback_run_trees` only as a monkeypatch target (`:3210-3274`, stubbed). None of the 21 creates a provider lock parent or destination.

Not measured on node-27, for a reason other than umask:
- 8 GRIB-decode cases (`test_e2e_ifs.py` ×2, `test_production_met_validation.py` ×6): no ecCodes on node-27, although `pyproject.toml` names node-27 as the `grib` oracle;
- 4 real-disk cases: fixture cycle past retention;
- the real-`data/Basins` smoke.

All three are recorded as out-of-scope findings.

Same run, #2403 RED at master (single test `tests/test_scheduler_backfill.py -k unreadable_index_is_not_memoized`):
- `umask=0002`: `1 failed` (`StateManagerError: provider_lock_parent_unsafe`, surfaced as `state_snapshot_index_write_failed`), `rc_2403_umask002=1`.
- `umask=0022`: `1 passed`, `rc_2403_umask022=0`.

## GRIB re-measure with node-27's GRIB env wired in (2026-09-23T13:54:51Z)

node-27 does have ecCodes: `/home/nwm/nhms-grib`, the `NHMS_GRIB_ENV_ROOT` of `infra/env/node27-download.env`, ships `lib/libeccodes.so` (ecCodes 2.47.0; `cfgrib` 0.9.15.1). Pytest simply does not wire it in: `ldconfig` cannot see it.

Setup: same worktree and SHA, with `LD_LIBRARY_PATH=/home/nwm/nhms-grib/lib`, `ECCODES_DIR`, `ECCODES_DEFINITION_PATH` (the `scripts/run_qhh_cycle.sbatch` shape), `NHMS_RUN_E2E=1`, `NHMS_RUN_GRIB=1`.

| File | `umask 002` | `umask 022` |
|---|---|---|
| `test_e2e_ifs.py` | 2 passed, 2 failed | 2 passed, 2 failed (same ids) |
| `test_production_met_validation.py` | 32 passed, 6 failed | 32 passed, 6 failed (same ids) |

- The 8 GRIB cases now decode, but fail on fixture/decoder expectations: `cfgrib variable mismatch … manifest expected 2t … dataset variables […]`, and the met lane asserts `'blocked' == 'ready'`. They fail identically under both umasks.
- So the umask dimension of #1632 IS measured for them, and it does not change the outcome.
- The failures themselves belong to #2594 (GRIB oracle wiring and fixture/version match), not to the provider gates.
- Raw output: `h-1632-grib.txt` (orchestrator scratch).

Final #1632 reading:
- Every one of the 21 files was executed under `umask 0002`, apart from the one opt-in `data/Basins` smoke.
- No outcome differs from `umask 022`.
- No provider-gate failure occurred.
- The 12 red cases are pre-existing and environment/fixture-bound. They are tracked in #2594 (8 GRIB) and #2595 (4 real-disk).
