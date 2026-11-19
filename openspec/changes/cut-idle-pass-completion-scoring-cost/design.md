Change surface: `services/orchestrator/scheduler_state_decision.py` (`_candidate_state_decision`,
  `_candidate_state_decision_evaluated`), `scheduler_state_evidence_owner.py` (`_candidate_state_evidence`),
  `scheduler_state_compat.py` (the compat wrapper must pass the keyword through), `scheduler_discovery.py`
  (`_cycle_completion_verdict` call site, context wiring in `scheduler_core.py` if a slot is added),
  `scheduler_runtime.py` (where `evidence["retention"]` is set), `scheduler_evidence_payload.py` (`_compact_retention`).
Must preserve:
  - every decision (`action`, `reason`) the ladder returns for a given (candidate, state);
  - `decision.evidence` on the submitting path, byte for byte;
  - the completion verdict (`complete` / `gap`) for every cycle, including pre-cutover cycles of recalibrated models
    (#1735), init-state conflict / absent / unverifiable branches (#1775) and stale predecessor identity;
  - journal read behaviour: number and kind of reads, containment checks, caches (#1567, #1661);
  - retention behaviour itself (what is planned, deleted, skipped, failed) and `frontier.protected_count`.
Must add/change:
  - completion scoring no longer builds or redacts a per-event copy;
  - pass evidence `retention` carries `skipped_by_reason`, `skipped_sample`, `skipped_sample_omitted` and no `skipped`.
Governing invariant: for the same journal state and model set, the set of cycles scored `complete` is identical before
  and after the change.
Sibling surfaces:
  - submit path deciders (`scheduler_candidates.py:347,810,1114`): must keep the default (full evidence);
  - `scheduler_state_compat.py` re-exports / wrappers: must not drop the keyword;
  - `file_orchestration_migration.py`, `chain_repository.py` callers of `candidate_state`: untouched;
  - readers of pass-evidence `retention.skipped`: none in `services/`, `scripts/`, `apps/`, `packages/` (grep); the
    over-limit compaction already removes the list, so readers already cope with its absence;
  - docs that tell an operator to read `retention.skipped`: update if any.
Seams under test: `_candidate_state_decision` (full vs opt-out on the same inputs); `cycle_completion_status` /
  `_cycle_completion_verdict` through the existing scheduler fakes; `ProductionScheduler.run_once()` pass evidence for
  the retention block.
Required evidence:
  - parity: over the existing completion truth-table states and at least one state per ladder exit
    (`production_identity_mismatch`, `active_slurm_job`, `active_duplicate_pipeline`, `terminal_completed_cycle`,
    `missing_upstream_artifact`, `cohort_membership_unprovable`, `resume_after_completed_stage`,
    `terminal_hydro_success`, `terminal_pipeline_success`), the opt-out decision equals the full decision in `action`
    and `reason`, and its evidence equals the full evidence with `pipeline_events` removed;
  - completion scoring of a cycle with events does not call the event sanitiser (spy on the call) and returns the same
    verdict as before, including a recalibrated-model cycle scoped out by lineage;
  - a submitting pass still publishes `pipeline_events` in candidate state evidence (existing tests stay green unedited);
  - retention block: counts per reason sum to `counts.skipped`; sample holds only non-`within_retention_window`
    entries, at most 50, with the overflow count; an over-limit pass keeps `skipped_by_reason`;
  - post-merge on node-22: idle `timing.pass.total_wall_ms`, `journal_read_attribution.totals` and evidence file size
    before/after; one submitting pass completes normally.
Non-goals: journal read-path changes; caching verdicts across passes; splitting `FileOrchestrationJournalRepository`;
  changing what retention deletes.
Review focus:
  1. Is there any ladder branch or helper that reads `evidence["pipeline_events"]` after it is built?
  2. Can any production path other than completion scoring end up with the opt-out (wrong default, wrapper, context)?
  3. Does the opt-out decision ever get published or persisted (it must not)?
  4. Does the retention projection lose an abnormal skip (non-window reason) silently?
