## MODIFIED Requirements

### Requirement: Completed-cycle skips SHALL be gated by journal-recorded predecessor identity

When readiness scoring would skip cycle T as already completed, the scheduler SHALL read the journal-recorded predecessor identity through one completed-pipeline identity authority: a completed matching hydro-run identity takes precedence; when it records no identity (including the state-save-QC cohort shape whose hydro row is absent or a non-completed placeholder), the authority SHALL fall back to the latest canonical-truth-order row for that candidate among explicit current-contract accepted-submit per-model candidate rows, then accept that row only when it is terminal-success and carries exactly one normalized identity entry bound to its own `array_task_id` and `model_id`. Selection SHALL be latest-first before qualification, so an older succeeded row cannot hide a newer failed, empty, or malformed row. Cohort master maps, marker-free historical jobs, other-model rows, run-manifest-backfilled identity, and public redaction projections SHALL provide no accessor identity. A completed hydro row's bare `state_id` alias MAY remain visible in the full mapping so the completion verdict preserves its legacy comparison semantics, but it SHALL NOT be promoted to `init_state_id` / `initial_state_id`; therefore the delegated legacy string accessor yields no token and both §8.7 predecessor wirings make no judgement from that alias alone. The legacy string accessor SHALL otherwise delegate to this same full mapping and expose only its historical `init_state_id` / `initial_state_id` aliases, so discovery-side predecessor scoring and candidate-side quarantine consume one token source while completion verdict may compare all recorded identity fields. When the token shares the expected base key (same source, model, and valid time) but carries a different lineage suffix, the scheduler SHALL treat T as not-canonical-ready without suppressing backfill selection (except as narrowed by the quarantine breaker requirement) and without mutating or deleting the journal entry; a matching token, absent or suffix-less identity, different base key (including earlier-valid-time fallback), no optional accessor, or unreadable/malformed current evidence SHALL preserve the existing no-judgement behavior. The authority SHALL read internal untruncated journal rows and SHALL NOT add `init_state_identities` to bounded candidate evidence or cycle-scope projection.

#### Scenario: Cohort per-model terminal identity activates both predecessor gates

- **WHEN** a state-save-QC completed cohort has no completed hydro identity and its latest current-contract per-model accepted-submit candidate row is terminal-success with exactly one normalized `init_state_identities` entry for that model
- **THEN** the full identity accessor returns that entry, the legacy string accessor returns its `init_state_id`, and discovery-side predecessor scoring and candidate-side quarantine judge the same token
- **AND** a same-base wrong-suffix token is a positive mismatch on both wirings, while a match, absent identity, suffix-less legacy token, different base key, missing accessor, or missing record remains no judgement
- **AND** if a newer candidate row is failed, empty, malformed, or belongs to another model, an older succeeded row cannot be selected in its place
- **AND** the scan is read-only, bypasses bounded candidate-state truncation/public redaction, and leaves the cohort master map outside every replicated candidate projection

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
