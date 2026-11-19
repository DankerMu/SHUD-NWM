Change surface: `services/orchestrator/scheduler_state_decision.py` (`_candidate_state_decision`,
  `_candidate_state_decision_evaluated`), `scheduler_state_evidence_owner.py` (`_candidate_state_evidence`),
  `scheduler_discovery.py` (both `candidate_state_decider` calls in `_cycle_completion_verdict`, `:362` and `:444`),
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
  - pass evidence `retention.skipped` omits `within_retention_window` entries and the block gains `skipped_summary`
    and `skipped_omits`.
Governing invariant: for the same journal state and model set, the set of cycles scored `complete` is identical before
  and after the change.
Sibling surfaces:
  - submit path deciders (`scheduler_candidates.py:347,810,1114`): must keep the default (full evidence);
  - `scheduler_state_compat.py`: the wrapper already forwards `**kwargs` (`:226-229`); no change expected;
  - `tests/test_file_orchestration_journal.py:7552`: the only two-argument decider double on the discovery slot;
  - `file_orchestration_migration.py`, `chain_repository.py` callers of `candidate_state`: untouched;
  - readers of pass-evidence `retention.skipped`: none in `services/`, `scripts/`, `apps/`, `packages/` (grep).
    Tests read it: `tests/test_production_scheduler.py:23797` filters for `pipeline_frontier_exempt` and keeps working
    unchanged; any test that reads `within_retention_window` entries from **pass evidence** moves to `skipped_summary`;
  - `services/orchestrator/cli.py:223` standalone receipt: unchanged, full list;
  - `docs/runbooks/two-node-deployment-overview.md:446` (frontier exemptions appear in `retention.skipped`): still
    true; add one sentence on `skipped_summary` / `skipped_omits`.
Seams under test: `_candidate_state_decision` (full vs opt-out on the same inputs); `cycle_completion_status` /
  `_cycle_completion_verdict` through the existing scheduler fakes; `ProductionScheduler.run_once()` pass evidence for
  the retention block.
Required evidence:
  - parity: over the existing completion truth-table states and at least one state per ladder exit
    (`production_identity_mismatch`, `active_slurm_job`, `active_duplicate_pipeline`, `terminal_completed_cycle`,
    `missing_upstream_artifact`, `cohort_membership_unprovable`, `resume_after_completed_stage`,
    `manual_retry_requested`, `retry_failed_candidate`, `permanent_failure_guard`,
    `manual_retry_required_after_cancelled`, `terminal_hydro_success`, `terminal_pipeline_success`) plus one state
    carrying `legacy_non_authoritative` rows, every state with at least one pipeline event: the opt-out decision
    equals the full decision in `action` and `reason`, and its evidence equals the full evidence with
    `pipeline_events` removed;
  - completion scoring through the **production binding** (`ProductionScheduler._discovery_context()`, not a
    hand-built context): the decision evidence completion scoring receives has no `pipeline_events` key, at both call
    sites; a positive control with the default shows the same state does yield `pipeline_events`, so the check cannot
    pass vacuously; verdicts equal the full-evidence verdicts for terminal success, init-state conflict, init-state
    absent / unverifiable, the packaged-IC branch, and a recalibrated-model cycle scoped out by lineage;
  - a submitting pass still publishes `pipeline_events` in candidate state evidence (existing tests stay green unedited);
  - retention block: `skipped_summary` counts sum to `counts.skipped` and carry the root; `skipped` holds every
    non-`within_retention_window` entry and none of the window ones; `skipped_omits` names the omitted reason; an
    over-limit pass keeps `skipped_summary` and `skipped_omits`; the standalone receipt still lists every entry;
  - post-merge on node-22: idle `timing.pass.total_wall_ms`, `journal_read_attribution.totals` and evidence file size
    before/after; one submitting pass completes normally.
Non-goals: journal read-path changes; caching verdicts across passes; splitting `FileOrchestrationJournalRepository`;
  changing what retention deletes.
Review focus:
  1. Is there any ladder branch or helper that reads `evidence["pipeline_events"]` after it is built?
  2. Can any production path other than completion scoring end up with the opt-out (wrong default, wrapper, context)?
  3. Does the opt-out decision ever get published or persisted (it must not)?
  4. Does the retention projection drop any skip other than `within_retention_window`, or lose a root's attribution?
