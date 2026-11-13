# Tasks — provenance-batch-journal-reuse (#3135)

- [x] 1.1 `publish_run_pipeline_job_provenance(..., repository: FileOrchestrationJournalRepository | None = None)`: use the passed instance after checking its root equals the verified journal root (else `PipelineJobProvenanceError("JOURNAL_REPOSITORY_ROOT_MISMATCH", ...)`); build a fresh one when `None` (unchanged path).
- [x] 1.2 `publish_runs_pipeline_job_provenance`: verify the root once, construct one repository, pass it to every per-run call; keep per-run try/except isolation and the summary shape.
- [x] 1.3 Tests (publisher test module): batch of >=3 same-cycle runs reads journal bytes from disk about once (assert via `journal_read_attribution` non-cached bytes or a counting reader; compare with the old per-run behaviour by constructing fresh repositories); append a job row for a later run between runs and assert it is published; root mismatch refused; single-run path unchanged.
- [x] 1.4 `select_ci_tests` routing still selects the provenance suites for `services/orchestrator/pipeline_job_provenance.py` (verify; add a row only if missing).
- [ ] 2.1 Post-merge (operator): node-22 ff-pull at a pass boundary; next compute pass receipt — `iter_publication_pipeline_jobs_by_cycle|cycle_replay` non-cached bytes and `state_save_qc` post-Slurm tail vs the 2026-11-13T02:00Z baseline (1.81 GB, ~37 min).
