# file-state-snapshot-index (delta)

## MODIFIED Requirements

### Requirement: Completed-cycle skips SHALL be gated by journal-recorded predecessor identity

When readiness scoring would skip cycle T as already completed, the scheduler SHALL compare the `init_state_id` recorded on the COMPLETED hydro run's row of the journal entry against the expected predecessor identity token for T (computed from the candidate's source, model, cycle time, expected predecessor `cycle_id`, and required lead hours); the recorded identity SHALL be read journal-only through the completed-pipeline identity accessor semantics — run-manifest-backfilled values and identities recorded only under a bare `state_id` alias yield no judgement — identically on both the discovery-side completion scoring and the candidate-side quarantine filter; when that recorded identity shares the expected token's base key (same source, model, and valid time) but carries a different lineage suffix, the scheduler SHALL treat T as not-canonical-ready without suppressing backfill selection (except as narrowed by the quarantine breaker requirement) and without mutating or deleting the journal entry, while a matching token, an absent or suffix-less recorded identity, a recorded identity with a different base key (including earlier-valid-time fallback warm-start states), or an identity recorded only on a non-completed (placeholder) hydro-run row superseded by a pipeline terminal SHALL preserve the existing skip behavior unchanged.

#### Scenario: Positive identity mismatch quarantines the completed entry

- **WHEN** the journal holds a completed cycle-T entry whose non-empty
  recorded `init_state_id` shares the expected predecessor token's base key
  (same source, model, and valid time T) but carries a different lineage
  suffix (wrong predecessor cycle or lead)
- **THEN** T is not reported as complete by readiness scoring
- **AND** T remains eligible for backfill selection (unless the quarantine
  breaker is engaged for T)
- **AND** the journal entry's on-disk content is byte-identical after the
  scoring pass (immutable audit entry)

#### Scenario: Matching identity preserves the completed skip

- **WHEN** the completed cycle-T entry's recorded `init_state_id` equals the
  expected predecessor identity token
- **THEN** T is skipped as completed exactly as before this change

#### Scenario: Absent or suffix-less recorded identity preserves legacy behavior

- **WHEN** the completed cycle-T entry records no `init_state_id`, or records
  a suffix-less legacy identity equal to the expected token's base key
- **THEN** no quarantine judgement is made and T is skipped as completed
  exactly as before this change

#### Scenario: Superseded placeholder hydro-run row is not judged

- **WHEN** the completed cycle-T entry's completion is decided by a pipeline
  terminal while its hydro-run row is a non-completed placeholder
  (`created`/`staged`/`submitted`) carrying a recorded `init_state_id` —
  such as under the `forecast_state_save_qc` terminal mode
- **THEN** no quarantine judgement is made and T is skipped as completed
  exactly as before this change

#### Scenario: Fallback warm start with a different base key is not quarantined

- **WHEN** the completed cycle-T entry's recorded `init_state_id` carries a
  different base key than the expected token — such as an earlier-valid-time
  fallback warm-start state legally selected under
  `NHMS_REQUIRE_FORECAST_WARM_START=false`
- **THEN** no quarantine judgement is made and T is skipped as completed
  exactly as before this change

#### Scenario: Run-manifest-backfilled identity yields no judgement on both wirings

- **WHEN** the completed cycle-T journal row records no `init_state_id`
  while the run manifest carries a wrong-suffix state id for the same run
  (a row the candidate-state assembly backfills from the manifest)
- **THEN** the candidate-side quarantine filter makes no judgement (the
  completed skip stands) and the discovery-side identity accessor returns
  no identity — the two wirings agree

#### Scenario: Bare state_id alias yields no judgement on both wirings

- **WHEN** the completed cycle-T journal row records a wrong-suffix identity
  only under the bare `state_id` key (neither `init_state_id` nor
  `initial_state_id`)
- **THEN** no quarantine judgement is made on either wiring and T is skipped
  as completed exactly as before this change

#### Scenario: terminal_completed_cycle skip is quarantined on positive mismatch

- **WHEN** a candidate skip with reason `terminal_completed_cycle` carries a
  durable-success hydro-run row whose journal-recorded `init_state_id` is a
  positive mismatch (same base key, wrong lineage suffix)
- **THEN** the quarantine filter declines the skip and produces the
  `retry_journal_predecessor_identity_mismatch` decision, exactly as for the
  `terminal_hydro_success` shape

## ADDED Requirements

### Requirement: Quarantine reruns SHALL be real forced resubmissions

The `retry_journal_predecessor_identity_mismatch` decision SHALL be a member of both forced-resubmit whitelists (`_FORCE_TERMINAL_RESUBMIT_DECISIONS` in `chain_forecast_orchestrator_cycle.py` and `force_replacement_decisions` in `chain_runtime_utils.py`), so that a repeated quarantine of the same cycle and model produces a replacement forecast submission with a new run identity instead of an idle resume that reuses the already-succeeded forecast job; the quarantine-breaker blocked decision SHALL NOT be a member of either whitelist.

#### Scenario: Second quarantine triggers a replacement submission

- **WHEN** cycle T for a model is quarantined a second time after its first
  quarantine rerun completed and re-recorded a stale identity
- **THEN** the orchestrator submits a replacement forecast run with a new
  run identity rather than resuming the previously succeeded forecast job

#### Scenario: Breaker-blocked decision cannot be revived by the whitelists

- **WHEN** the quarantine breaker has demoted a candidate to its blocked
  decision
- **THEN** neither whitelist matches that decision string and no replacement
  submission is produced for it

### Requirement: Quarantine reruns SHALL prefer the expected predecessor lineage when selecting the exact warm-start state

When `NHMS_REQUIRE_FORECAST_WARM_START` is not true and a basin carries quarantine evidence (`journal_predecessor_identity`), the exact warm-start lookup at `before_time == cycle_time` SHALL first query the state-snapshot provider with the expected predecessor lineage — `cycle_id` derived from the candidate cycle time minus the evidence's `required_lead_hours`, and `lead_hours` equal to that value — and select the lineage-matching entry when a usable one exists; the lineage SHALL be a preference, not a filter: whenever the lineage-preferred lookup does not yield a USABLE snapshot (no lineage-matching entry, or a matching entry whose `usable_flag` is false), and equally whenever the lineage-preferred snapshot is subsequently rejected by downstream lineage validation or QC, the selection SHALL retry today's unfiltered exact lookup at the cycle time exactly once (a one-shot preference reset that does not advance the selection cursor past the cycle time) so the rerun lands on today's selection byte-identically (never degrading to a zeroed cold start, since the file-index state manager offers no earlier-valid-time fallback), leaving non-convergent shapes to the quarantine breaker. Selection paths without quarantine evidence SHALL pass no lineage arguments and behave byte-identically to before this change, and the PostgreSQL provider SHALL keep ignoring the lineage arguments (the §8.7 quarantine loop exists only in file-journal mode).

#### Scenario: One rerun converges when the expected-lineage entry exists

- **WHEN** a multi-interval cadence (`0,6,12`) fixture holds both a
  wrong-lineage and the expected-lineage state entry at valid time T, the
  wrong-lineage entry's `state_id` sorts strictly before the expected one
  under plain string ordering, and the quarantine rerun's basin carries
  `journal_predecessor_identity` evidence
- **THEN** the rerun selects the expected-lineage entry, records the expected
  token, and the next scoring pass makes no quarantine judgement

#### Scenario: Lineage miss falls back to today's selection, converging via the breaker

- **WHEN** the quarantine rerun's expected-lineage entry does not exist at
  valid time T, or exists only with `usable_flag` false
- **THEN** the lookup repeats the unfiltered exact selection and picks the
  same wrong-lineage entry as before this change (no cold start), the rerun
  re-records the stale token, and the non-convergent shape is terminated by
  the quarantine breaker requirement

#### Scenario: Downstream rejection of the preferred entry resets to today's selection

- **WHEN** the lineage-preferred snapshot at valid time T is usable but is
  then rejected by downstream lineage validation (for example a
  model-package version mismatch after a registry upgrade) or by the QC
  hook
- **THEN** the selection performs the one-shot preference reset, repeats
  the unfiltered exact lookup at the cycle time, and selects the same
  wrong-lineage usable entry as before this change — never a zeroed cold
  start

#### Scenario: Non-quarantine selection is unchanged

- **WHEN** a basin without quarantine evidence selects its warm-start state
  under `NHMS_REQUIRE_FORECAST_WARM_START` not true
- **THEN** the provider receives no lineage arguments and the selection is
  byte-identical to before this change

### Requirement: A non-convergent quarantine SHALL be broken once a quarantine rerun re-records the stale identity

A forecast cohort reservation whose basin carries the `retry_journal_predecessor_identity_mismatch` decision SHALL stamp quarantine-rerun provenance onto the cohort MASTER row (the affected model ids, written by the reservation writer — the §8.7 scoring and filtering surfaces themselves remain strictly read-only). When the stale recorded `init_state_id` for one cycle and model has been re-recorded by at least one terminal-success cohort MASTER row carrying that provenance for that model — counted read-only from the journal (per-model terminal rows that reconcile copies the identity onto are excluded, so one submission's master row plus its per-model terminal row count as one), never from the bounded candidate-state payload; masters minted by non-quarantine replacements (for example a missing-run-manifest or missing-forecast-output resubmission) carry no such provenance and SHALL NOT arm the breaker; journals written before this change carry no provenance field and SHALL leave the breaker disengaged — the scheduler SHALL stop producing the quarantine retry: the candidate-side filter SHALL demote the decision to a blocked decision carrying a typed reason, the recorded and expected identity tokens, the occurrence count, and a manual-retry-required retry policy; the discovery-side backfill selection SHALL exclude a cycle from the single oldest-first execution slot only when every model keeping that cycle a gap is breaker-engaged (a cycle with any genuinely incomplete model SHALL keep taking the slot), while still reporting the excluded cycle as a gap (never as complete) and emitting a not-selected evidence entry that carries both identity tokens; no journal row SHALL be written or deleted, and an unavailable or failed occurrence count SHALL leave the breaker disengaged and the quarantine retry decision unchanged (fail toward liveness).

#### Scenario: Breaker demotes the quarantine to blocked after a provenance-stamped rerun re-records the token

- **WHEN** a completed quarantine rerun (its master row stamped with
  quarantine-rerun provenance for the model) has re-recorded the same stale
  `init_state_id` that the current positive mismatch observes
- **THEN** the candidate-side decision is the blocked decision with the
  recorded token, the expected token, the provenance-stamped occurrence
  count, and `manual_retry_required` true — while before any stamped rerun
  completes the decision stays the quarantine retry

#### Scenario: Non-quarantine replacements do not pre-arm the breaker

- **WHEN** the journal holds two or more terminal-success masters recording
  the same stale token but none carries quarantine-rerun provenance for the
  model (for example the second master came from a missing-run-manifest or
  missing-forecast-output replacement)
- **THEN** the first quarantine judgement is the ordinary quarantine retry,
  and the convergence layer gets its rerun before any fail-stop

#### Scenario: Pre-change journals leave the breaker disengaged

- **WHEN** the cycle's journal rows were written before this change and
  carry no provenance field
- **THEN** the breaker stays disengaged and the quarantine retry behavior
  is unchanged

#### Scenario: Breaker-engaged gap releases the backfill execution slot

- **WHEN** the oldest available incomplete cycle is breaker-engaged and a
  later available gap exists for the same source
- **THEN** the later gap is selected for execution, the breaker-engaged
  cycle appears as a not-selected evidence entry carrying both identity
  tokens, and its completion status remains gap

#### Scenario: One submission's master and terminal rows count as one

- **WHEN** a single completed provenance-stamped quarantine rerun has
  written the stale token onto its cohort master row and reconcile has
  copied the identity onto the model's per-model terminal row
- **THEN** the provenance-stamped occurrence count for that token is
  exactly 1 — the reconcile copy is never double-counted — and, that count
  witnessing one failed convergence attempt, the breaker engages; the same
  two rows without the provenance stamp count 0 and leave the quarantine
  retry unchanged

#### Scenario: Mixed cycle with a genuinely incomplete model keeps the slot

- **WHEN** one model of cycle T is breaker-engaged while another model of
  the same cycle still has work the scheduler could progress — whether it
  has no completed pipeline at all, or has its own positive identity
  mismatch whose provenance-stamped count has not engaged the breaker
- **THEN** cycle T still takes the backfill execution slot and executes
  normally for the model that is not breaker-engaged

#### Scenario: Unavailable occurrence count leaves the breaker disengaged

- **WHEN** the journal occurrence count cannot be read (no accessor on the
  repository, no rows, or unreadable rows)
- **THEN** the breaker stays disengaged and the candidate-side decision
  remains `retry_journal_predecessor_identity_mismatch`

#### Scenario: Journal remains untouched by the breaker

- **WHEN** the breaker engages for cycle T
- **THEN** the journal's on-disk content is byte-identical after the pass
