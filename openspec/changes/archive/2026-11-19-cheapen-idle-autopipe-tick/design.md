# Design

Change surface: `services/orchestrator/pipeline_job_provenance.py` (`import_discovered_pipeline_job_provenance`),
`scripts/node27_autopipeline.py` (phase 3.4 call site, `--summary-mode`, summary emission),
`scripts/node27_autopipe_cron.sh` (rotation, `--summary-mode tick`), `infra/env/node27-ingest.example`, runbooks that
read `autopipe.log`.

Must preserve:
- Import semantics for every run that is imported: one transaction per run, identity immutability, monotonic
  lifecycle, fail-closed conflicts, per-run failure isolation, `unavailable` for a missing sidecar, the same failure
  reason for the same input (today an oversized sidecar gives `SIDECAR_UNREADABLE` and a symlinked one
  `ObjectStoreError`; `SIDECAR_TOO_LARGE` is a publisher-side code).
- `import_run_pipeline_job_provenance` and `import_runs_pipeline_job_provenance` behave as today for every caller
  that does not pass a state path; the backfill CLI output is unchanged.
- Default stdout of `node27_autopipeline.py` (`--summary-mode full`): one pretty JSON document; tests and hand runs
  parse it. The only shape change in `full` mode is inside `runs.job_provenance` of a pass that calls the importer:
  it gains `skipped_unchanged` and `skip_state` (plus `skip_state_saved` with a state path), and with a state path
  skipped runs are counted and not listed in `runs`.
- The two fixed `job_provenance` dicts keep their exact current shape, without the new keys: the `--seed-only` /
  default dict (`{"imported": 0, "unavailable": 0, "failed": 0, "runs": []}`, script `:2953`) and the
  importer-exception fallback (`:2966-2975`).
- The runbook reads keep working on a tick-mode log: `"declines_active": N` (every tick, in the digest),
  `"stage": "forcing_handoff"`, `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`, `<run_id>` and `stats_guard.authority`
  detail (present whenever a run failed or was declined or the authority leg did or deferred anything, because such a
  tick is not quiet), `autopipe: (start|done|phase=)`.
- The existing `node27-ingest-boundary` scenario "Ingest evidence distinguishes data-plane and display-plane health":
  every tick's log still identifies the ingest role, stage shape, object-store root, DB host/port without secrets,
  the counts and the return code — the digest carries them.
- The connect call sites of `scripts/node27_autopipeline.py` (`_connect` callers, `frontier-chunk-statistics-freshness`
  spec): the presence read uses the `connect` callable already handed to the importer, with the importer's own
  `fallback_application_name`; no new `_connect` caller in the script.

Must add/change:
- State file `<AUTOPIPE_WORK_ROOT>/pipeline-job-provenance-import-state.json`, schema
  `nhms.node27.pipeline_job_provenance_import_state.v1`:
  `{"schema": ..., "runs": {"<run_id>": {"sidecar_sha256": "<hex>", "jobs": {"<job_id>": "<row_sha256>"}}}}`.
  `jobs` has one entry for **every** job of the validated sidecar (inserted, updated, unchanged and ignored-stale
  alike). The value is the row fingerprint: SHA-256 of a canonical JSON of the columns the importer reads and
  writes (`job_id, run_id, cycle_id, job_type, slurm_job_id, array_task_id, model_id, status, stage, submitted_at,
  started_at, finished_at, exit_code, retry_count, error_code, error_message, log_uri, created_at, updated_at`),
  timestamps as UTC ISO-8601 instants. Fingerprints are always computed from rows **read from the database** by one
  function (the row read below), at the end of the pass for the runs imported in it and at the start of a pass for
  the check — never from what the import intended to write — so both sides normalise identically.
  Written with a temp file in the same directory and `os.replace`, only when its content changed. The tick lock
  serialises the timer's passes. A hand run over the full run set that does not take the lock can race the file:
  the last writer wins, every surviving marker is still a row read made after a committed import, and a lost marker
  costs one re-import. Hand runs over the full set should hold the timer's lock.
- Skip predicate, per run, all three: (a) a marker exists; (b) SHA-256 of the sidecar bytes read in this pass equals
  `sidecar_sha256`; (c) for every entry of `jobs` the row read returns that `job_id` with the recorded fingerprint.
  Row read: the columns above `FROM ops.pipeline_job WHERE job_id IN (...)`, at most 1,000 ids per statement,
  read-only (no `FOR UPDATE`, no advisory lock). It runs at most twice per pass, each time on one connection opened
  through the caller's `connect`: before the imports, only when at least one run satisfies (a) and (b); and after
  them, only when at least one run was imported, over the jobs of the imported runs. If the second read fails, the
  runs imported in this pass get no marker (they are imported again next pass).
- The sidecar bytes are read once per run in a discovered pass with a state path, inside that run's failure
  isolation (a sidecar that cannot be read fails that run only, with today's reason code); the same bytes feed the
  digest and, if the run is not skipped, the import. The marker therefore records the digest of the bytes the
  committed import parsed.
- Marker lifecycle: written after an import returns `imported`; removed when the run is `failed` or `unavailable`;
  dropped for any run id not in this pass's request (retention). A skipped run keeps its marker.
- Shared rows: a cycle-scoped job appears in every sidecar of its cycle. Runs imported in the same pass all record
  the row as it stands at the end of that pass. When a sidecar in a later pass changes such a row, the other runs of
  the cycle fail (c) once, go through the ordinary import (which reports `ignored_stale` or `unchanged`) and get
  their markers refreshed. This converges after one pass (rows only move forward) and is bounded by the runs of one
  cycle.
- Degradation: state file missing, unreadable, not JSON, wrong schema, or wrong shape → no markers (`skip_state`
  `empty`). Presence read raises → no run is skipped this pass (`skip_state` `presence_check_failed`). State write
  fails → warning on the logger, `skip_state_saved` false, the pass result is otherwise unaffected.
  `skip_state_saved` is true whenever the file on disk equals this pass's state (written, or already identical).
- A sidecar with no jobs has an empty `jobs` map: it is skipped on unchanged bytes alone, without a row read.
- The autopipeline hands the importer the state path only on a pass that discovers the full run set: none of
  `--only-basin`, `--only-cycle`, `--limit` given and `--sources` left at its default. A narrowed hand run imports
  its runs without reading or writing the state (`skip_state` `disabled`), so it neither drops the markers of the
  runs it did not request nor races the timer for the file. The wrapper's standing exclusions
  (`--exclude-basins`, `--exclude-model-ids`, `--direct-grid-only`) do not narrow in this sense.
- The `[job-provenance]` progress line on stderr also reports the skipped count.
- Discovered-pass summary gains `skipped_unchanged` (int), `skip_state` (`disabled` without a state path, else
  `loaded` | `empty` | `presence_check_failed`) and `skip_state_saved` (bool, absent when disabled). `status`,
  `imported`, `unavailable`, `failed` keep their meaning; `runs` lists only runs that were not skipped.
- `--summary-mode {full,tick}`, default `full`. In `tick` mode the final summary (the one emitted at the end of
  `main`, which `--seed-only` also reaches) is preceded by the digest line and printed only if the tick is not
  quiet. The three preflight exits (`:2545`, `:2575`, `:2656`) print their summary in full in both modes, with no
  digest. Digest schema and the quiet predicate are pinned in the `node27-ingest-boundary` delta; the digest's
  `job_provenance.skipped_unchanged` is the summary's value, or 0 where the summary dict has no such key. The digest
  also carries `cycle_floor` and `skipped_older_than_floor` (the bring-up runbook confirms the floor from the timer
  log of an idle node).
- Wrapper rotation: `AUTOPIPE_LOG_MAX_BYTES` (default 67108864; `0` disables; anything that is not a non-negative
  integer → default) and `AUTOPIPE_LOG_KEEP` (default 5; anything that is not a positive integer, including `0` →
  default). After the lock is taken and before `autopipe: start`: if `$LOG` is a regular file of at least the bound,
  drop `$LOG.<KEEP>`, shift `$LOG.<i>` to `$LOG.<i+1>`, remove any `$LOG.<n>` with n > KEEP left by an earlier larger setting, move `$LOG` to `$LOG.1`, and write
  `autopipe: rotated log size_bytes=<n> keep=<k>` as the first line of the new file. A rotation error is logged and
  never fails the tick.

Governing invariant: a run is skipped only if importing it now would be a no-op — same bytes as the last committed
import, and every row that import wrote or compared against is still there, column for column as it was read back
after that import; in every other case, including any doubt about the state, the run goes through the unchanged import path.

Sibling surfaces:
- Producer (node-22 publisher): unchanged; it rewrites a sidecar by atomic replace, which changes the bytes only when
  the evidence changes (measured: no churn).
- Other writers of `ops.pipeline_job` on node-27: none besides this importer (the compute plane is database-free and the
  DB-backed orchestrator paths do not run on node-27). Row loss (re-initdb, manual delete) and any change of an
  importer-visible column (restore to another backup, including one that only lacks a later `log_uri` enrichment at
  an unchanged `updated_at`; a sibling sidecar's update; a manual edit) are detected through the fingerprint.
- Manifest and `hydro.hydro_run` identity checks run only for imported runs. A skipped run was checked when its
  marker was written; the manifest of a published run is immutable.
- Consumers: display API reads `ops.pipeline_job`; unaffected. Runbook readers: listed under Must preserve and in
  task 4.1.
- Failure paths: importer exception fallback in the autopipeline (unchanged shape); state degradation above.

Seams under test: `import_discovered_pipeline_job_provenance` (with the suite's sqlite adapter and a real object-store
directory), `node27_autopipeline.main` stdout in both modes, the wrapper as a subprocess, one real-PostgreSQL pass.

Required evidence: see `tasks.md` Evidence Floor.

Non-goals: connection reuse across imported runs; changing the
display-ready seed check; compressing rotated generations; the output of the coverage-backstop and MVT-prewarm
phases; changing the summary schema of `full` mode beyond `job_provenance`.

Review focus:
1. The skip predicate can never skip a run whose import would change a row (bytes changed, rows lost or replaced,
   import failed last time, sidecar appeared since, a shared cycle-scoped row whose newer copy lives in a run that is
   no longer requested).
2. The marker digest is of the bytes that were actually imported, `jobs` covers every job of the sidecar, and both
   sides of the fingerprint comparison come from the same row-read function.
3. `full` mode stdout and the two fixed `job_provenance` shapes are unchanged.
4. A failing or declined run still leaves its stage and reason in the log every tick.
5. Rotation cannot lose the current tick's output or run without the lock.
