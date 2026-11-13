# provenance-batch-journal-reuse (#3135)

## Why

On node-22 a catch-up pass spends ~155 min of Python wall time for ~85 min of Slurm work. The largest single sink is
the pre-copyback provenance publication (`chain_forecast_execution.py:1177` →
`publish_runs_pipeline_job_provenance`): every run in the batch builds a fresh `FileOrchestrationJournalRepository`
(`pipeline_job_provenance.py:208`) and replays the whole cycle journal through
`iter_publication_pipeline_jobs_by_cycle`. Measured on pass `scheduler_2026111302_a14d3192b66b`: 1.81 GB, 10822 read
calls, **0 cache hits** (75% of the pass's 2.43 GB journal reads), and `state_save_qc` ran ~37 min past its last Slurm
job. The repository already owns a stat-identity read cache (`file_orchestration_journal.py:7800`, exact
`(mtime_ns, size, inode)` match, 64 MiB / 4096 entries) that a fresh instance cannot use.

Owner decision 2026-11-13: ship as fast as possible (A1 concurrency raise + this B1).

## What

- `publish_run_pipeline_job_provenance` accepts an optional `repository` (a `FileOrchestrationJournalRepository`
  already bound to the verified journal root); when omitted it behaves exactly as today.
- `publish_runs_pipeline_job_provenance` verifies the journal root once, builds ONE repository and passes it to every
  run in the batch. Per-run failure isolation, summary shape, lock scope and publication content are unchanged.
- Content equivalence: a cache hit requires an identical stat signature, so any append or atomic replacement between
  runs misses and re-reads; the bytes each run sees are identical to a fresh-instance read at that instant.
- Tests prove: (a) N same-cycle runs in one batch read the journal from disk ~once (attribution `bytes` vs
  `cache_hit_bytes`, or an instrumented reader), not N times; (b) a journal append between two runs is visible to the
  later run; (c) existing publisher/copyback tests stay green; (d) a repository bound to a different root is refused
  (fail closed) rather than silently used.

Non-goals: planning-stage replay cost (separate profile, B2); changing provenance content or lock scope; the A1
concurrency env change (operator config, already applied 2026-11-13T04:44Z).

Fixture level: expanded (shared orchestrator read path over persisted journal state, concurrency with other cohort
threads writing the same cycle journal). Upstream: none.
Blast radius: provenance is diagnostic display evidence and its failures never raise into copyback
(`chain_forecast_execution.py:1280`); a wrong cache would publish stale job rows for a run, caught by tests (b).
Selected risk packs: Persisted/shared state (journal read cache), Concurrency (cohort threads appending the same cycle
journal), Error handling (per-run isolation preserved).
Evidence floor: `uv run pytest -q tests/test_pipeline_job_provenance_publisher.py
tests/test_pipeline_job_provenance_copyback.py tests/test_pipeline_job_provenance_importer.py
tests/test_file_orchestration_journal_read_cache.py tests/test_select_ci_tests.py` (node-27 + local);
`uv run ruff check .`; `openspec validate provenance-batch-journal-reuse --strict --no-interactive`; post-merge
node-22 pass receipt (attribution for `iter_publication_pipeline_jobs_by_cycle|cycle_replay` and `state_save_qc`
tail before/after).
