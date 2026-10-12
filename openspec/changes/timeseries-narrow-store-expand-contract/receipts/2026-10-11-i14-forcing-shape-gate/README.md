# I14 forcing shape gate — receipt (fixture I14-1993-task82.md D5)

Status: **measured, gate exit 1 (RED) on one reader that 000066 cannot affect; owner decision pending.**
This receipt does not authorise applying `000066` on node-27. Production was not modified.

## What was run

- When: 2026-10-12T03:16:37Z (node-27 local 11:16), read-only.
- Where: node-27 production PG (`nhms-db`, PostgreSQL 15.2, TimescaleDB 2.10.2), catalog at ledger head 000065
  (`legacy_table_present: true` — this is the PRE-contract catalog).
- Code: verify checkout `/home/nwm/nwm-2785-verify` at `cdc1cba4ef9449648efbf4d35a23538a0faa4519` (PR #2904 head at
  the time). `/home/nwm/NWM` stayed at `b0831d6ce`.
- Session: `default_transaction_read_only=on`, `statement_timeout=120s`; DSN from the ingest role's env file, never
  printed. Whole run took about 8 s; md0 `[UU]` before and after.
- Command: `python -P probe.py --out probe.json --plan-only-for qhh_production_bootstrap.dynamic_forcing_count`,
  then `python -P gate.py probe.json`.
- Files: `probe.json` (raw plans), `gate.txt` (the judge's output, verbatim).

Identity probed: run `fcst_gfs_2026101100_dg_0883c7e9c1006c6fd347df500315e9df`, basin `basins_qhh`,
cycle 2026-10-11 00:00Z. Narrow fact table: 30 chunks, 9 compressed; compression segmentby
`(forcing_version_key, station_key)`, orderby `(variable_e, valid_time)` (read from
`timescaledb_information.compression_settings` the same day).

## Result per reader

| # | Reader | Fact Seq Scans | Exec ms | Verdict |
|---|---|---|---|---|
| 1 | `forecast_store.latest_product_station_source` (QHH fallback, fast path forced off) | 0 | 1446.2 | PASS — ×0.90 of the 2026-09-20 legacy baseline 1612.573 ms |
| 2 | `forecast_store.station_forcing_membership` | 0 | 0.11 | PASS |
| 3 | `forecast_store.station_series_rows` | 0 | 2.7 | PASS |
| 4 | `forecast_store.forcing_readiness_overall` | 0 | 19.6 | PASS |
| 5 | `forecast_store.forcing_readiness_variable_rows` | 0 | 56.2 | PASS |
| 6 | `display_coverage.station_sample_rows` (ANALYZE leg = derived SELECT) | 0 | 1392.3 | PASS |
| 7 | `best_available.forcing_inputs` | 0 | 57.2 | PASS |
| 8 | `qhh_production_bootstrap.dynamic_forcing_count` | 30 (21 chunks + 9 compressed) | not measured | **FAIL** |

All seven measured readers ran entirely from shared buffers (`shared_read=0`).

Gate line: `SHAPE GATE: RED (8 expected, 1 violating, 0 incomplete)`, exit 1.

## Reader 8

`workers/model_registry/qhh_production_bootstrap.py:832-838` counts every fact row of a model's stations. It carries
no predicate on the fact table's keys, so a scan of every chunk is its shape by construction — fixture D5(a)
("zero Seq Scan across the eight statements") assumed a key predicate this reader never had.

- It reads `met.forcing_station_timeseries` and `met.met_station` only. `000066` drops
  `met.forcing_station_timeseries_legacy` and `met.forcing_version.timeseries_store`; the reader names neither, so
  its plan is the same before and after the contract. This is a baseline fact, not a regression.
- It was run with `--plan-only-for`, so `gate.py` also reports its ANALYZE leg as skipped. With that flag the gate
  cannot exit 0 for this reader; read the other seven by their own PASS lines, not by the exit code.

Criterion D5(a) is therefore met by 7 of 8 readers and not met by reader 8. D5(b) is met (×0.90). The fixture is not
amended here: whether to accept reader 8 as a documented exception or to give it a bounded shape first (a separate
issue against that reader) is the owner's call before the production window.

## Deviations from the fixture's probe description

Recorded in `probe.json` and in PR #2904's 偏离记录: the coverage reader's ANALYZE leg is a derived SELECT (an INSERT
cannot be ANALYZEd read-only); the QHH fallback is reached by switching the fast path off for that one call;
`best_available` is routed onto the probe's read-only connection; a `compress_hyper_*` Seq Scan under a fact chunk's
DecompressChunk is counted as a fact Seq Scan (on production this rule produced no violation for readers 1–7).

## Real-database tests (same checkout, same day)

node-27, throwaway databases (`nhms_it_%`, 0 before and after), `pytest -m integration` over 15 files including
`tests/test_forcing_timeseries_contract_integration.py`, the #2621 two-state test, `tests/test_migrations.py` and the
expand-era suites: **148 passed, 0 skipped** (82 deselected are the non-integration members of those files). The
`timescaledb_210` compressed-chunk contract test is among the 148.
