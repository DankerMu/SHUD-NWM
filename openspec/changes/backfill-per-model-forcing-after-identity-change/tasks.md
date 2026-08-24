# Tasks

## Evidence Floor

- `uv run pytest -q tests/test_node22_backfill_forcing_for_model_ids.py tests/test_node22_manual_retry_failed_runs.py`
- `uv run ruff check .`
- `openspec validate backfill-per-model-forcing-after-identity-change --strict --no-interactive`
- node-22 live: backfill receipt with `status_counts` all `verified` and
  `rebound_models_skipped: 0`
- node-22 live: manual-retry receipt, and a scheduler pass in which the marked
  run leaves `blocked` and its forecast succeeds

`nhms-compute-scheduler.timer` stays disabled until #1832 is resolved, by
operator decision; that is not a gate on this change.

## 1. Backfill

- [x] 1.1 Pair two registry manifests by `(sp_att_path, source_id)`; refuse a
      diverged key set and a changed `basin_version_id`.
- [x] 1.2 Skip, with evidence, any pair whose `station_bindings` differ beyond
      the identity prefix.
- [x] 1.3 Discover `(model_id, cycle)` pairs holding the old id but not the new,
      deriving the source path segment as `normalize_source_id(x).lower()`.
- [x] 1.4 Replay the producer per work item; default to a dry run.
- [x] 1.5 Verify equivalence: `shud/*.csv` byte-identical, data members equal
      after identity normalisation, manifests excluded.
- [x] 1.6 `--jobs` for bounded concurrency (each item writes its own directory).

## 2. Manual retry

- [x] 2.1 Preview the row the marker would act on, distinguishing the per-run
      row from the cohort master; default to preview-only.
- [x] 2.2 Record one marker per named run through `record_manual_repair`;
      report refusals rather than raising.
- [x] 2.3 Unit tests for 2.1 and 2.2.
- [x] 2.4 A preview-time refusal under `--execute` is a refusal, not a preview:
      it is recorded as `refused` and the command exits non-zero, while the
      other named runs in the same invocation are still processed. (Found by
      2.3: the first implementation reported such refusals as `preview_only`
      and exited 0, contradicting this change's own spec delta.)

## 3. Documentation

- [x] 3.1 Runbook hop 5, first half: why a copy is wrong and how to replay.
- [x] 3.2 Runbook hop 5, second half: an already-failed run does not self-heal;
      use the marker channel, never a journal edit.

## 4. Local verification

- [x] 4.1 `uv run pytest -q tests/test_node22_backfill_forcing_for_model_ids.py tests/test_node22_manual_retry_failed_runs.py`
- [x] 4.2 `uv run ruff check .`
- [x] 4.3 `openspec validate backfill-per-model-forcing-after-identity-change --strict --no-interactive`

## 5. Live receipts (node-22)

- [x] 5.1 Backfill: 16/16 `verified`, `rebound_models_skipped: 0`.
- [x] 5.2 Manual retry: 8 markers, each resolving to a per-run reconciled row
      (`job_fcst_..._forecast_reconciled_34817_<n>`, array tasks 0-7), never the
      cycle's forecast cohort master.
- [x] 5.3 Passes in which the marked runs leave `blocked` and run: the heihe
      probe drove the full chain (convert 34848, forcing 34849_0, forecast
      34850_0, state_save_qc 34851_0, all `COMPLETED 0:0`), warm-started from
      the cloned state, and wrote the `2026-08-23T12:00:00Z` state into both
      indexes; the same pass left the other seven `blocked`, which is what
      proves the marker is per-run. Fanning out to those seven: 7/8 basins
      `succeeded` with fresh next-cycle states.
- [x] 5.4 The eighth basin (`hetianhe`) fails for an unrelated cause -- a SHUD
      solver NaN under its source `GEOL_DMAC`, tracked in #1832. Its forcing
      backfill and marker both worked; the failure is downstream of this change
      and does not belong to it.
