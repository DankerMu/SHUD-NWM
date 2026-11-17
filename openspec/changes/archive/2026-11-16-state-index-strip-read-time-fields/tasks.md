# Tasks — state-index-strip-read-time-fields (#3123)

- [x] 1.1 In `publish_state_snapshot_index`, build `payload["entries"]` from copies of the entries with
  `index_generated_at` and `object_evidence` removed. Use one module-level tuple of read-time field names, and reuse
  it at the existing read-side strips (`_copyback_raw_entries` and the repair read-back) without removing them.
- [x] 1.2 Tests per E2, including re-seeding the two existing legacy-strip test groups as raw bytes. Record red-on-old.
- [x] 1.3 Confirm that no publish caller relies on persisting either field, including the provider-refresh renewal path
  (`scripts/scheduler_refresh/runner.py:555`).
- [x] 1.4 Add `NHMS_SLURM_SCHEDULER_STATE_INDEX` to the env templates (proposal What 2) and keep the template and
  drift-table tests green.
- [x] 1.5 Update the runbook (`known-issues-state-index.md` ~:525-528 and ~:885-892) per proposal What 3.
