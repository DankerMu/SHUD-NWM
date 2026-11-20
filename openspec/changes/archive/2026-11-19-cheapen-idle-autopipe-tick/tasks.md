# Tasks

## 1. Provenance import (implementer)

- [x] 1.1 `services/orchestrator/pipeline_job_provenance.py`: `import_discovered_pipeline_job_provenance` takes an
      optional `state_path`. Without it the function behaves and returns exactly as today plus `skipped_unchanged: 0`
      and `skip_state: "disabled"`. With it: load markers (degrade to none per `design.md`), read each requested
      run's sidecar bytes once through the existing safe object-store reader, skip per the three-part predicate,
      import the rest through the existing per-run path with those same bytes, then write the state file atomically
      if its content changed. The marker's `jobs` map has every job of the validated sidecar with the row
      fingerprint read back after the imports of the pass (design.md: one row-read function for both sides). The
      sidecar read happens inside the per-run failure isolation. `import_run_pipeline_job_provenance` and `import_runs_pipeline_job_provenance` keep
      their signatures and results for existing callers.
- [x] 1.2 The row read: through the caller's `connect` (same kwargs the importer already passes), the importer's
      column list, at most 1,000 `job_id`s per statement, no `FOR UPDATE`, no advisory lock. Before the imports (only
      when at least one run matches marker and digest): any exception → no run skipped,
      `skip_state: "presence_check_failed"`. After the imports (only when at least one run was imported): any
      exception → no marker for the runs imported in this pass, `skip_state_saved` reflects whether the file was
      written.
- [x] 1.3 The 256-run chunking and per-run failure isolation of the discovered pass stay as they are.

## 2. Autopipeline and wrapper (implementer)

- [x] 2.1 `scripts/node27_autopipeline.py` phase 3.4 passes
      `state_path=Path(WORK_ROOT) / "pipeline-job-provenance-import-state.json"` on a pass that discovers the full
      run set (no `--only-basin`, `--only-cycle`, `--limit`; `--sources` at its default) and no state path otherwise.
      The `[job-provenance]` progress line also reports the skipped count. The importer-exception fallback and
      the `--seed-only` `job_provenance` dicts keep their exact current shape.
- [x] 2.2 `--summary-mode {full,tick}` (default `full`); digest line and quiet predicate exactly as in the
      `node27-ingest-boundary` delta; the digest goes through `redact_payload` like the summary. The three preflight
      exits (`:2545`, `:2575`, `:2656`) print in full in both modes with no digest; `--seed-only` reaches the final
      summary and follows the digest rule.
- [x] 2.3 `scripts/node27_autopipe_cron.sh`: rotation per `design.md` (after the lock, before `autopipe: start`), and
      `--summary-mode tick` on the ingest invocation. Header comment updated.
- [x] 2.4 `infra/env/node27-ingest.example`: `AUTOPIPE_LOG_MAX_BYTES` and `AUTOPIPE_LOG_KEEP` documented as optional
      (commented, with the defaults).

## 3. Tests (implementer)

`tests/test_pipeline_job_provenance_importer.py` (the suite's sqlite adapter, a real object-store directory):

- [x] 3.1 `test_discovered_pass_skips_a_run_whose_sidecar_and_rows_are_unchanged`: second pass → the run is not in
      `runs`, `skipped_unchanged == 1`, and the connection log of the second pass contains the presence read and no
      statement of the per-run import (no `hydro.hydro_run` read, no `FOR UPDATE`).
- [x] 3.2 `test_discovered_pass_reimports_a_run_whose_sidecar_bytes_changed`: rewrite the sidecar with a newer job
      status → imported, `updated == 1`, marker digest replaced.
- [x] 3.3 `test_discovered_pass_retries_a_failed_run_and_never_marks_it`: an identity conflict fails the run → no
      marker; fix the sidecar → next pass imports it.
- [x] 3.4 `test_discovered_pass_reimports_when_a_marked_row_is_missing`: delete one projected row → the run is
      imported again in the next pass (`inserted == 1`).
- [x] 3.4a `test_discovered_pass_reimports_when_a_marked_row_differs`: parametrised — (i) one projected row
      rewritten with an older `updated_at` and status (a restore to an earlier backup) → the run is imported again
      and the row is updated to the sidecar's version; (ii) one projected row whose `log_uri` is set back to NULL at
      the same `updated_at` (a restore from between the null-log import and the enrichment) → the run is imported
      again and the `log_uri` is back.
- [x] 3.4b `test_discovered_pass_restores_a_shared_cycle_row_from_the_older_sidecar`: two runs of one cycle carry
      the same cycle-scoped job, run B with the newer copy (run A reports `ignored_stale` for it); the next pass
      requests only A, after the shared row was deleted → A is imported (not skipped) and the row exists again.
- [x] 3.4c `test_discovered_pass_refreshes_sibling_markers_after_a_shared_row_update`: A and B imported in one
      pass → both skipped in the next; then B's sidecar updates the shared row → in that pass B is imported and A is
      skipped (its rows had not changed when the pass started); in the next pass A is imported once (`ignored_stale`
      or `unchanged`), and in the pass after that both are skipped.
- [x] 3.5 `test_discovered_pass_treats_missing_corrupt_or_foreign_state_as_empty`: parametrised over no file,
      non-JSON bytes, wrong `schema`, `runs` not an object → `skip_state == "empty"`, every run imported, a valid
      state file afterwards.
- [x] 3.6 `test_discovered_pass_imports_a_sidecar_that_appears_later`: first pass `unavailable` (no marker), sidecar
      published, second pass imports it.
- [x] 3.7 `test_discovered_pass_drops_markers_of_runs_no_longer_requested`.
- [x] 3.8 `test_discovered_pass_marker_is_the_digest_of_the_imported_bytes`: the sidecar is replaced after the pass
      has read it (hook on the reader) → the marker digest equals the digest of the bytes read, and the next pass
      imports the run again.
- [x] 3.9 `test_discovered_pass_presence_read_failure_skips_nothing`.
- [x] 3.9a `test_discovered_pass_unreadable_sidecar_fails_only_its_run`: parametrised over a symlinked sidecar and
      an oversized one, with a state path and a second healthy run → the bad run is `failed` with the reason the
      same input gives without a state path (asserted equal; today `ObjectStoreError` / `SIDECAR_UNREADABLE`), has
      no marker, and the
      healthy run is imported (first pass) or skipped (second pass).
- [x] 3.10 `test_discovered_pass_without_state_path_imports_every_run_and_writes_no_file`; the existing bounded-import
      and backfill-CLI tests stay green unmodified.

`tests/test_node27_autopipeline_tick_summary.py` (new; reuse the `_prepare_autopipe` / `_run_main` helpers of
`tests/test_node27_autopipeline_handoff.py` by import or by moving them to a shared helper module — no copy):

- [x] 3.11 `test_tick_mode_prints_exactly_one_digest_line_on_a_quiet_tick`: one stdout line, parses as JSON,
      `quiet is True`, the raw line contains `"declines_active": 0`.
- [x] 3.12 `test_tick_mode_prints_the_full_summary_when_a_run_failed`: digest `quiet is False`; the JSON document
      after the first line has the failed run's `stage` and `error` in `runs.failed_runs`.
- [x] 3.13 `test_tick_mode_is_not_quiet_for_each_single_cause`: parametrised, one cause at a time — a declined run,
      a seeded basin, a seed failure, provenance `imported` 1, provenance `unavailable` 1, provenance `failed` 1,
      `declines_active` 1, `declines_active` None, stats-guard authority `failed`, stats-guard authority `analyzed`
      non-empty.
- [x] 3.14 `test_full_mode_stdout_is_one_json_document_without_a_digest`.
- [x] 3.15 `test_autopipeline_hands_the_importer_a_state_path_under_the_work_root`, and
      `test_autopipeline_withholds_the_state_path_on_a_narrowed_pass` (parametrised over `--only-basin`,
      `--only-cycle`, `--limit 1`, `--sources gfs`: the importer is called with no state path).
- [x] 3.16 `test_preflight_blocked_summary_is_printed_in_full_in_tick_mode` (no digest line).
- [x] 3.16a `test_tick_mode_seed_only_pass_prints_a_digest`: `--seed-only --summary-mode tick` → first line is a
      digest whose `job_provenance` is `{imported 0, unavailable 0, failed 0, skipped_unchanged 0}`; and
      `test_tick_mode_digest_of_the_importer_exception_fallback_is_not_quiet` (`failed` equals the run count,
      `skipped_unchanged 0`).
- [x] 3.16b `test_tick_mode_digest_identifies_the_ingest_plane`: `ingest.role`, `ingest.stage_shape`,
      `ingest.config_source`, `ingest.database` and `object_store_root` are present on a quiet tick and no value of
      the writer DSN's password appears in the line.

Wrapper, in the file that already runs it as a subprocess (`tests/test_node27_autopipeline_preflight.py`):

- [x] 3.17 `test_wrapper_rotates_a_log_at_the_bound_and_keeps_bounded_generations`: `AUTOPIPE_LOG_MAX_BYTES=64`,
      `AUTOPIPE_LOG_KEEP=2`, pre-existing `log`, `log.1`, `log.2` → after the tick: old `log` content in `log.1`, old
      `log.1` in `log.2`, no `log.3`, new `log` starts with the rotation line then `autopipe: start`.
- [x] 3.18 `test_wrapper_leaves_a_log_under_the_bound_and_a_zero_bound_alone` (parametrised), and
      `test_wrapper_falls_back_to_defaults_for_invalid_rotation_settings` (parametrised: `AUTOPIPE_LOG_KEEP` in
      `0`, `-1`, `abc` → five generations kept; `AUTOPIPE_LOG_MAX_BYTES` in `-1`, `abc` → the 64 MiB default, so a
      small log is not rotated).
- [x] 3.19 `test_wrapper_does_not_rotate_when_the_lock_is_held`.
- [x] 3.20 `test_wrapper_passes_tick_summary_mode_to_the_ingest_invocation`.

Real PostgreSQL (`tests/test_pipeline_job_provenance_importer_integration.py`, same fixtures and markers as the
existing tests there):

- [x] 3.21 `test_discovered_pass_with_state_skips_then_reimports_after_row_loss`: pass 1 imports two runs; pass 2
      skips both and leaves `ops.pipeline_job` byte-identical (`xmin` of every row unchanged); delete one row; pass 3
      re-inserts it and skips the other run; then set one row's `log_uri` to NULL without touching `updated_at` and
      pass 4 imports that run again.

## 4. Fixture and docs (orchestrator)

- [x] 4.1 Runbooks that read the log: `docs/runbooks/tier-node27-timeseries-storage.md` (the three greps at
      `:4597-4599`: the digest line carries `"declines_active"`, rotated generations are `autopipe.log.<n>`),
      `docs/runbooks/tier-node27-timeseries-storage.md:6015` (`stats_guard.authority` detail is in the full summary,
      which a tick prints whenever the authority leg analysed, deferred or failed; the digest has only its status),
      `docs/runbooks/production-ops/coverage-freshness-alert.md:165,317` (`*.log` does not match rotated generations),
      `docs/runbooks/production-ops/service-bringup.md:51-67` (what an idle tick shows; `cycle_floor` is in the
      digest, basin names only in the full summary),
      a paragraph in `artifacts-and-storage.md` next to the work-root listing: the state file needs no action after
      a restore (rows that are missing or differ are re-imported), may be deleted at any time to force one full
      pass, and a hand run over the full run set should hold the timer's lock,
      `docs/runbooks/production-ops/parse-failure-residency-alert.md:93,107`,
      `docs/runbooks/production-ops/artifacts-and-storage.md:153` (rotation bound instead of manual truncation).
- [x] 4.2 Tick these tasks, archive the change, write real Purpose paragraphs for the two touched specs if they still
      say TBD.

## Deviations recorded during implementation and review

- 3.9a: on the importer side an oversized sidecar fails with `SIDECAR_UNREADABLE` and a symlinked one with
  `ObjectStoreError` (`SIDECAR_TOO_LARGE` is a publisher-side code); the test pins equality with the no-state path.
- 3.18 is two tests (`…_invalid_keep`, `…_invalid_max_bytes`); the max-bytes one pins the 64 MiB default from both
  sides with a sparse file (review P2).
- Added in review: rotation removes generations above a lowered `KEEP` (test
  `test_wrapper_rotation_removes_generations_above_a_lowered_keep`); the digest carries `cycle_floor` and
  `skipped_older_than_floor`; tests for the batched row read, a zero-job sidecar and the third preflight exit.
- 3.21 first failed on node-27 on its own assertion (the shared seed already holds one `ops.pipeline_job` row); the
  assertion became a subset check and the file's four tests pass against real PostgreSQL.
- The restore / hand-run note of 4.1 lives in `production-ops/artifacts-and-storage.md` next to the work-root
  listing, not in the storage runbook's restore section.

## Risk packs

- Public API / CLI / script entry — selected: `--summary-mode`; default output pinned by 3.14, 3.16.
- Config / project setup — selected: two optional wrapper env keys; 2.4, 3.17, 3.18.
- File IO / path safety / overwrite — selected: state file (atomic replace, degrade on damage: 3.5, 3.8) and log
  rotation (3.17–3.19); the sidecar is still read through the safe reader (existing symlink test stays green).
- Schema / columns / units / field names — selected: state-file schema, digest schema, `job_provenance` keys; pinned
  in the spec deltas and by 3.1, 3.11.
- Auth / permissions / secrets — not selected: no new credential or role; the digest is redacted like the summary.
- Concurrency / shared state / ordering — selected: republication during a pass (3.8); writers serialised by the tick
  lock; rotation only under the lock (3.19).
- Resource limits / large input / discovery — selected: this is the point of the change; E4, E5.
- Legacy compatibility / examples — selected: bounded import, backfill CLI, `full` mode and the two pinned
  `job_provenance` shapes unchanged (3.10, 3.14, existing tests).
- Error handling / rollback / partial outputs — selected: 3.3, 3.5, 3.9; state write failure does not fail the pass.
- Release / packaging / dependency compatibility — not selected: no dependency change.
- Documentation / migration notes — selected: 4.1.

## Evidence Floor

- E1 local: `uv run ruff check . --no-cache`; `uv run pytest -q tests/test_pipeline_job_provenance_importer.py
     tests/test_node27_autopipeline_tick_summary.py tests/test_node27_autopipeline_handoff.py
     tests/test_node27_autopipeline_preflight.py tests/test_node27_autopipeline_connection_bounds.py
     tests/test_node27_connection_attribution.py tests/test_node27_connection_attribution_delegated.py
     tests/test_pipeline_job_provenance_publisher.py tests/test_pipeline_job_provenance_copyback.py`.
- E2 `openspec validate cheapen-idle-autopipe-tick --strict --no-interactive`.
- E3 node-27, temporary detached worktree on the PR head, throwaway `nhms_it_*` database: the E1 pytest list plus
     `tests/test_pipeline_job_provenance_importer_integration.py` (3.21 runs, not skipped).
- E4 node-27 after deploy, from `autopipe.log`: the first tick is the cold pass (`skip_state` `empty`, all runs
     imported, recorded with its `elapsed_sec`); the next three idle ticks have `phase=ingest elapsed_sec <= 45`
     (baseline 218–223) and digest `job_provenance.skipped_unchanged` equal to `discovered_runs`, `imported 0`.
     Samples taken outside the 04:25–05:25Z compression window.
- E5 node-27 log volume: bytes the ingest invocation appends to `autopipe.log` on an idle tick (from the
     `autopipe: start` line up to and excluding the `phase=ingest` line) `<= 4096` (baseline 877,333; the coverage
     backstop and MVT prewarm phases, 323 and 1,104 bytes on the baseline tick, are not changed); the first tick
     after deploy rotated the 518 MB file to `autopipe.log.1`; `grep -o '"declines_active": [0-9]*' autopipe.log | tail`
     returns values from idle ticks. Measured growth over the first 24 h is posted on the issue when available.
- E6 node-27: the first real ingest tick after deploy completes `rc=0`, its digest is `quiet false` and is followed by
     the full summary, provenance for the new cycle's runs is imported on that or the following ticks
     (`ops.pipeline_job` rows present for a sampled new run), and the display frontier advances
     (`hydro.hydro_run` newest published cycle per source, read-only query).
