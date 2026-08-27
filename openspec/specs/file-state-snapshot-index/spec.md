# file-state-snapshot-index Specification

## Purpose
TBD - created by archiving change pin-env-override-state-lineage-blocks. Update Purpose after archive.
## Requirements
### Requirement: The warm-start env override SHALL NOT admit candidates blocked by state-lineage invariants

With `NHMS_REQUIRE_FORECAST_WARM_START=false` and a valid in-window cutover declaration, the §8 gate SHALL still block a candidate for which no checkpoint sits at the expected predecessor identity key, the index holds no usable entry at the candidate's `valid_time`, and the transition decision is `BLOCK_PREDECESSOR_PENDING` — whether the usable state-index history sits strictly earlier than or strictly later than the candidate cycle (typed reason `state_snapshot_index_prior_checkpoint_missing_after_history`) — and a candidate for which current-generation history exists and the checkpoint at the expected predecessor identity key — `valid_time` equal to the candidate cycle, producing `cycle_id` of cycle minus the source cadence, matching `lead_hours` — carries a different generation token (typed reason `state_snapshot_index_generation_mismatch`, transition decision `BLOCK_WRONG_GENERATION`); within these preconditions the env override never bypasses state-lineage blocks.

#### Scenario: Env override does not admit a missing predecessor

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false`, the cutover
  declaration is valid, in-window, and its `effective_cycle_utc` is
  strictly earlier than the candidate cycle, and usable state-index
  history exists strictly earlier than the candidate cycle (the gate's
  history signal is generation-agnostic) but holds no checkpoint at the
  expected predecessor identity key (a candidate at the declaration's
  effective cycle with old-generation-only history is instead admitted
  as declared cold start — see the sibling scenario "Old-generation
  checkpoints do not block declared cold start")
- **THEN** the candidate is blocked with typed reason
  `state_snapshot_index_prior_checkpoint_missing_after_history`
- **AND** the recorded transition decision is `BLOCK_PREDECESSOR_PENDING`
- **AND** no candidate is admitted

#### Scenario: Env override does not admit a missing predecessor when no earlier usable history exists

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false`, the cutover
  declaration is valid, in-window, and its `effective_cycle_utc` is
  strictly earlier than the candidate cycle, current-generation usable
  state-index entries exist only at `valid_time` strictly later than the
  candidate cycle (so the gate's strictly-earlier history probe reports
  no history while the generation-scoped transition signal still sees
  current-generation history), and no checkpoint sits at the expected
  predecessor identity key
- **THEN** the candidate is blocked with typed reason
  `state_snapshot_index_prior_checkpoint_missing_after_history`
- **AND** the recorded transition decision is `BLOCK_PREDECESSOR_PENDING`
- **AND** the blocked evidence records `state_history.history_exists = false`
- **AND** no candidate is admitted

#### Scenario: Env override does not admit a wrong-generation checkpoint

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false`, the cutover
  declaration is valid and in-window, current-generation history exists,
  and the checkpoint at the expected predecessor identity key carries a
  generation token different from the candidate's (a wrong-generation
  checkpoint with NO current-generation history at a declaration's
  effective cycle is instead admitted as declared cold start — see the
  sibling scenario "Old-generation checkpoints do not block declared
  cold start")
- **THEN** the candidate is blocked with typed reason
  `state_snapshot_index_generation_mismatch`
- **AND** the recorded transition decision is `BLOCK_WRONG_GENERATION`
- **AND** no candidate is admitted

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

### Requirement: Copyback merge SHALL scope destination-side object verification and checkpoint copying to the winning merged source entries

The state-snapshot-index copyback merge SHALL keep today's source-side validation unchanged: the full source index (before `authoritative_run_ids` filtering) is validated with object verification against the private reference root, failing closed on any missing or checksum-divergent source object. Destination-index reading SHALL retain structural validation (unreadable or non-object payloads fail closed; schema, payload checksum, entry limits, required fields, URI safety, and identity/state-id uniqueness checks preserved) but SHALL NOT require destination-side object existence for pre-existing entries. Checkpoint copying SHALL iterate only the source entries that won the merge for their identity key — a source entry that loses the merge collision to a later destination entry SHALL NOT have its object copied, and a pre-existing destination entry whose object has been archived from the shared root SHALL be carried through the merge unchanged with its object NOT re-copied (no resurrection against the archive contract). The published index SHALL remain the full merged entry set (pre-existing destination entries plus winning source entries) — scoping applies to verification and copying only, never to the published set. The merge-internal index publish SHALL NOT re-run full-index object verification; integrity of newly published entries is guaranteed by the per-entry source checksum verification and post-write read-back comparison, which SHALL remain unchanged. Merge collision semantics, locking, and compare-and-swap preimage semantics SHALL remain byte-identical. Other callers of the index publish function SHALL keep their existing verification behavior, and the publish function's defaults SHALL NOT change.

#### Scenario: Archived destination objects no longer block new entries

- **WHEN** the destination index contains historical entries whose objects have been archived from the shared root and a copyback merges new authoritative source entries whose objects verify against the private reference root
- **THEN** the merge succeeds, the new entries and their objects are published, and the historical entries are preserved unchanged in the published index

#### Scenario: Archived objects are not resurrected

- **WHEN** a copyback merge completes against a destination index holding entries whose shared-root objects were archived
- **THEN** those objects are not re-copied to the shared root by the merge

#### Scenario: Losing source entries do not overwrite shared objects

- **WHEN** a merged source entry loses its identity-key collision to a destination entry with a later created_at
- **THEN** the destination entry is published for that key and the losing source entry's object is not copied to the shared root

#### Scenario: The published set is never narrowed

- **WHEN** a copyback merge publishes the destination index
- **THEN** the published entry count equals the pre-existing destination entries plus the net-new winning source entries

#### Scenario: Source-side integrity is not weakened

- **WHEN** a source entry has a missing or checksum-divergent object under the private reference root
- **THEN** the merge fails closed exactly as today

#### Scenario: Corrupt destination index still fails closed

- **WHEN** the destination index is unreadable or not a JSON object
- **THEN** the merge fails closed exactly as today

### Requirement: A receipted idempotent copyback replay SHALL exist for failed state-index copybacks

An operator-invoked replay tool SHALL re-run the state-index copyback for an explicit run-id set or for the runs of one or more explicit cycles, resolved from the source index by matching each entry's flat optional `cycle_id` field after normalizing the requested cycle identifier to the production lowercase-source form. It SHALL expose exactly two object-store roots (private reference and shared destination, defaulting to the production environment variables) with the index paths derived from them, and SHALL refuse equal or overlapping roots. It SHALL default to dry-run — a read-only preview that does not invoke the merge, changes no index content, and copies no objects — and require an explicit enforce flag to invoke the real merge code path used by production copyback. An empty run-id resolution SHALL exit non-zero with a structured reason and SHALL NOT invoke the merge. An enforce run SHALL refuse to proceed — exiting non-zero before any merge invocation, index write, or object copy — when the derived destination index file does not exist, unless bootstrap is explicitly allowed by a dedicated flag. Enforce runs SHALL be idempotent (a repeated enforce run publishes no new entries and copies no objects) and SHALL write a JSON receipt (schema-versioned, recording mode, resolved run ids, entry counts before and after, and per-checkpoint outcomes) under the receipt root named by its environment variable. Refusal semantics SHALL be limited to failures that provably left the destination index uncommitted: a merge-raised error may be reported as a refusal only when its reason is on an explicit pre-commit allowlist (preimage-changed, validation, and collision classes whose raise point precedes the destination compare-and-swap); any merge-raised error not on that allowlist SHALL be classified as commit-uncertain and SHALL be reported with a distinct non-refusal reason, so that unknown future failure modes fail safe as uncertain rather than as refusals. Every committed or commit-uncertain outcome — including a post-merge destination read-back failure or a receipt failure — SHALL exit non-zero with a distinct post-merge failure reason that does not claim refusal, SHALL run the post-merge evidence chain (destination read-back, entry-preservation verification, and receipt write) as far as the failure allows, and SHALL emit the merge summary, as far as known, on stdout. An enforce run SHALL verify after the merge that the published destination index contains every entry identity the pre-merge destination read observed, and SHALL report a loss as a distinct post-merge failure rather than success; when a loss verdict and a receipt failure occur in the same run, the loss reason SHALL take precedence in the reported failure with the receipt failure recorded in its details. The tool SHALL NOT touch the orchestration journal, the registry, or canonical-readiness providers.

#### Scenario: Backlogged entries are recovered idempotently

- **WHEN** the replay tool is enforced for a cycle whose earlier copyback failed closed
- **THEN** the missing entries enter the destination index with their objects copied, a receipt records the before/after counts, and a second enforce run reports zero new entries and copies no objects

#### Scenario: Dry-run changes nothing

- **WHEN** the replay tool runs without the enforce flag
- **THEN** no index content change and no object copy occurs, and the receipt/preview reports the resolved run ids and would-be new entry count

#### Scenario: Empty resolution fails closed

- **WHEN** the requested cycles or run ids resolve to no source-index entries
- **THEN** the tool exits non-zero with a structured reason and the destination index is not written

#### Scenario: Missing destination index refuses enforce

- **WHEN** the replay tool is enforced against a destination root whose derived index file does not exist and bootstrap has not been explicitly allowed
- **THEN** the tool exits non-zero with a structured reason before any merge invocation, and no index or object is written under the destination root

#### Scenario: Receipt failure after a successful merge is reported distinctly

- **WHEN** the merge succeeds but the receipt cannot be written
- **THEN** the tool exits non-zero with a post-merge failure reason that does not claim refusal and the merge summary is emitted on stdout

#### Scenario: Post-merge read-back failure is reported distinctly

- **WHEN** the merge succeeds but the post-merge destination read-back fails
- **THEN** the tool exits non-zero with a post-merge failure reason that does not claim refusal, and the merge summary as far as known is emitted on stdout

#### Scenario: Destination entries lost across the merge are reported as failure

- **WHEN** the destination index observed before the merge vanishes or loses entries before the merge commits, so the published index no longer contains every previously observed entry identity
- **THEN** the enforce run exits non-zero with a distinct post-merge failure reason instead of reporting success

#### Scenario: Untyped merge exceptions are commit-uncertain

- **WHEN** the merge call raises an exception that is not one of the known typed error classes carrying a reason, such as a bare OSError raised from inside the merge internals without a classifying wrapper (lock-teardown failures are no longer an example: they now arrive typed with release-uncertain semantics and take the commit-uncertain path with a real reason)
- **THEN** the tool classifies the outcome as commit-uncertain, runs the post-merge evidence chain, writes the receipt, and exits with the distinct non-refusal reason carrying a synthetic error identifier instead of crashing with an unclassified traceback

#### Scenario: Commit-uncertain merge failures do not claim refusal

- **WHEN** the merge raises an error whose reason is not on the pre-commit allowlist, such as a durable-replace or post-read uncertainty where the destination index may already hold the new content
- **THEN** the tool exits non-zero with a distinct non-refusal reason, runs the post-merge evidence chain as far as the failure allows, and does not report the run as refused

#### Scenario: Loss verdict outranks receipt failure

- **WHEN** an enforce run detects lost destination entries and the receipt also cannot be written
- **THEN** the reported failure reason is the entry-loss reason, with the receipt failure recorded in its details

### Requirement: Provider lock-release failure after the destination commit MUST be classified as commit-uncertain

A provider destination-lock release failure SHALL be raised as a classified
error carrying a phase that identifies writes inside the lock scope as
already completed (release-uncertain semantics), never as an unclassified
OS-level exception, and a release failure SHALL NOT mask an exception
already propagating from the lock body. The state-index copyback merge's
callers SHALL treat that classification as committed-or-uncertain, never as
a provable refusal: the operator replay tool SHALL report it through its
existing commit-uncertain path (non-refusal exit code, merge summary on
stdout, receipt recording the uncertain commit state and the release-failure
reason), and the natural orchestration copyback path SHALL surface it as a
structured copyback error whose code is distinct from the fail-closed
merge-failure code, reaching the copyback pipeline event with that distinct
code rather than escaping as a bare exception with no event. Sibling users
of the same provider lock keep their existing exception contracts, with the
release failure now arriving classified instead of bare. This requirement
governs release-period error classification only: lock acquisition
semantics, blocking behavior, merge collision semantics, and
compare-and-swap preimage semantics remain unchanged (the byte-identical
locking clause of the copyback-scope requirement is to be read as covering
acquisition and CAS preimage semantics, which this requirement does not
touch), and a release failure never leaks the lock or parent file
descriptors — a subsequent same-process lock acquisition on the same path
succeeds.

#### Scenario: merge caller can prove the commit happened despite the release failure

- **WHEN** the destination compare-and-swap has published the merged index
  and the subsequent lock release fails with an OS-level error
- **THEN** the merge raises a classified provider error with
  release-uncertain semantics and the destination index bytes are the merged
  content, so a caller can assert the commit as a fact rather than infer
  from the exception type

#### Scenario: replay reports commit-uncertain, not a refusal and not a bare crash

- **WHEN** the operator replay tool runs enforce and the merge fails only in
  the lock-release period after the commit
- **THEN** the tool exits with its committed/uncertain exit code and status,
  emits the known merge summary on stdout, writes the receipt with the
  uncertain commit state naming the release-failure reason, and never exits
  with an unclassified traceback, an empty stdout, or a refusal status

#### Scenario: natural copyback path emits an event with a distinct code

- **WHEN** the orchestration copyback stage hits the same release-period
  failure
- **THEN** the copyback raises a structured error whose code differs from
  the fail-closed merge-failure code and the stage writes the copyback
  pipeline event carrying that code, instead of a bare exception with no
  event

#### Scenario: release failure never masks the in-flight body error

- **WHEN** the lock body raises a pre-commit classified error and the lock
  release also fails during unwinding
- **THEN** the propagated exception is the body's pre-commit error, with the
  release failure suppressed rather than replacing it

### Requirement: Destination-CAS uncertain and postcommit merge failures SHALL be classified commit-uncertain on the natural copyback path

The natural orchestration copyback path SHALL classify a state-index merge failure by the error's self-described phase, regardless of carrier: a bare classified provider error carrying a phase, or a state-manager error wrapping one with the phase preserved in its evidence. A failure whose phase places it at or past the destination compare-and-swap — release-uncertain, replace-uncertain, or postcommit — SHALL surface as the distinct commit-uncertain copyback error code, never as the fail-closed merge-failure code; only a failure with no self-described phase or with a pre-commit phase keeps the fail-closed code. This mirrors the operator replay tool's refusal contract (only audited pre-commit raise points prove the destination index unchanged), so the two operator surfaces give the same verdict for the same failure and future uncertain phases fail safe as uncertain. A postcommit restored-previous failure — where the provider verifiably rolled the destination back to its prior bytes — SHALL still classify as commit-uncertain, matching the replay tool's exclusion of it from the pre-commit allowlist: the merged bytes were transiently visible and "nothing happened" is not provable to the caller. Both the commit-uncertain and the fail-closed copyback errors SHALL carry the underlying failure reason in their details alongside the existing error text, so runbook triage can key on the reason under either code. The #1193 release-uncertain classification requirement is unchanged; this requirement widens the same distinct code to the remaining post-CAS family.

#### Scenario: rewrapped replace-uncertain failure surfaces as commit-uncertain with the committed fact assertable

- **WHEN** the destination compare-and-swap's atomic replace has executed but its durability or identity confirmation fails, and the provider error is rewrapped by the state manager with reason `provider_replace_uncertain` and phase `replace_uncertain` in its evidence
- **THEN** the natural copyback path raises the commit-uncertain copyback code (not the fail-closed code) with `error_reason` `provider_replace_uncertain` in details, and the destination index bytes hold the merged entries, so a caller or test can assert the commit as a fact

#### Scenario: post-CAS read-back failure surfaces as commit-uncertain

- **WHEN** the post-CAS read-back verification fails and no verified rollback succeeds, rewrapped with reason `provider_postread_failed`
- **THEN** the natural copyback path raises the commit-uncertain copyback code with that reason in details

#### Scenario: verified rollback still classifies commit-uncertain

- **WHEN** the post-CAS read-back fails but the provider restores the previous destination bytes and verifies the restoration (reason `provider_restored_previous`, phase postcommit)
- **THEN** the natural copyback path raises the commit-uncertain copyback code with that reason in details, the destination index bytes are the previous content, and the classification matches the replay tool's non-refusal verdict for the same reason

#### Scenario: pre-commit failures keep the fail-closed code

- **WHEN** the merge fails with a pre-commit phase (for example a preimage change) or with a state-manager index-validation reason that carries no phase
- **THEN** the copyback raises the fail-closed merge-failure code exactly as before, with the underlying reason now present in details

#### Scenario: runbook triage keys on one coherent verdict table

- **WHEN** an operator triages a copyback failure event per the production runbook
- **THEN** the fail-closed code means provably pre-commit (no unresolved uncertain family rides under it), and the commit-uncertain code enumerates the release-uncertain, replace-uncertain, and postcommit reasons with the entry-count check as the common next step

### Requirement: Predecessor-pending blocked evidence SHALL carry an operator self-heal signal

Blocked evidence for typed reason `state_snapshot_index_prior_checkpoint_missing_after_history` emitted by the §8 gate path (`registry_cutover_transition`-carrying emission in `scheduler_generation_gate.strict_warm_start_evidence`) SHALL carry `self_heal_expected` — true if and only if the emitted §8.6 predecessor's own exact warm-start verification succeeds, computed by the same provider verification the predecessor's own gate would run (`strict_warm_start_evidence` at `valid_time = required_prior_cycle_time` with the candidate's package checksum and lead hours, requiring `ready=True`) so that identity, generation/lineage, `usable_flag`, and state-object availability/content are all covered — and `operator_action_required` (its negation). Any verification shortfall — wrong-generation entry at the slot, missing/corrupt state object, absent entry, malformed evidence — SHALL resolve to `operator_action_required=True` (fail toward escalation, never toward false reassurance). The signal is single-level by definition: it answers "will the single-level §8.6 backfill close THIS candidate's gap", and operator triage reads the discovered successor's record; a deeper chain stall is signaled by the successor's `operator_action_required=True`, not by fields on emitted-predecessor records. The evidence SHALL also carry a compact `self_heal_probe` sub-object recording the probe's `ready` and typed `reason` so operators can see why self-heal was ruled out.
When `operator_action_required` is true the evidence SHALL name the operator
action (`backfill_predecessor_state`) and the runbook path
`docs/runbooks/scheduler-dbfree-typed-reasons.md`. The `failure` block
(`retryable=True`, `permanent=False`) and the gate decision itself SHALL remain
unchanged. The legacy (pre-§8, checksum-less/no-declaration or untrusted-index)
emission site is out of scope: it returns passthrough `None` for the
no-earlier-history geometry; on the `history_exists=True` geometry it emits the
typed reason WITHOUT the signal fields, and operators fall back to the manual
criteria documented in the runbook (field absence is not a self-heal guarantee).
`operator_action_required` SHALL survive the bounded-evidence summarization
tier (retained in `_BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS`) so the runbook's
single-boolean triage remains executable on summarized passes.
The signal is CONSERVATIVE at the declared-cutover boundary: when the
predecessor slot coincides with a declaration's effective cycle
(`T == effective_cycle + lead_hours`), the emitted predecessor is admitted by
the transition matrix via `cold_declared_cutover` while the warm-start probe
still reads not-ready — `operator_action_required=True` is then a false
positive in the safe direction; operator triage SHALL confirm the emission
record (`records[].status == "emitted"`) before manually scheduling the
predecessor cycle.

#### Scenario: declared-cutover boundary reads operator-action but the predecessor is already admitted

- **GIVEN** an in-window declaration with `effective_cycle_utc == T − required_lead_hours`,
  old-generation-only history, and `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true`
- **WHEN** the successor at `T` blocks with the typed reason and
  `emit_predecessor_candidates` runs against the real §8 gate
- **THEN** the successor's evidence carries `operator_action_required=True`
  (conservative false positive) while the emission record carries
  `status="emitted"` — the two-step triage (boolean, then emission record)
  resolves it without manual scheduling

#### Scenario: no-earlier-history geometry flags operator action

- **GIVEN** a candidate whose state index holds only entries strictly later
  than the candidate cycle (`state_history.history_exists=False`)
- **WHEN** the §8 gate emits
  `state_snapshot_index_prior_checkpoint_missing_after_history` blocked
  evidence
- **THEN** the evidence carries `operator_action_required=True`,
  `self_heal_expected=False`, `operator_action="backfill_predecessor_state"`
  and a runbook path, while `failure.retryable` stays `True`

#### Scenario: exact-predecessor-state geometry stays self-heal

- **GIVEN** a candidate whose state index holds a usable current-generation
  state exactly at `T − required_lead_hours` (the emitted predecessor's own
  warm-start state)
- **WHEN** the same typed reason is emitted
- **THEN** the evidence carries `self_heal_expected=True` and
  `operator_action_required=False`

#### Scenario: multi-cycle gap geometry flags operator action despite earlier history

- **GIVEN** a candidate whose only usable strictly-earlier state sits at
  `T − 2·required_lead_hours` or earlier (`state_history.history_exists=True`
  but `latest_usable_state.valid_time != required_prior_cycle_time`)
- **WHEN** the same typed reason is emitted
- **THEN** the evidence carries `operator_action_required=True` and
  `self_heal_expected=False` — a ≥2-cycle outage does not self-heal under
  single-level §8.6 backfill and must not be labeled as converging

#### Scenario: wrong-generation entry at the predecessor slot flags operator action

- **GIVEN** a candidate whose state index holds an old-generation `usable_flag=true`
  entry exactly at `T − required_lead_hours` (current-generation history sits
  elsewhere or nowhere)
- **WHEN** the same typed reason is emitted
- **THEN** the evidence carries `operator_action_required=True` and
  `self_heal_expected=False` — the emitted predecessor would block on
  `state_snapshot_index_generation_mismatch` and never run, and a
  wrong-generation entry at the slot never diverts the successor's own typed
  reason

#### Scenario: missing state object at the predecessor slot flags operator action

- **GIVEN** a candidate whose state index holds a current-generation
  `usable_flag=true` entry exactly at `T − required_lead_hours` whose state
  object is absent from the object store
- **WHEN** the same typed reason is emitted
- **THEN** the evidence carries `operator_action_required=True`,
  `self_heal_expected=False`, and a `self_heal_probe.reason` of
  `state_snapshot_index_object_missing`

#### Scenario: emitted predecessor reproduces the blocked shape under a real §8 gate

- **GIVEN** `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true` and a successor
  blocked with `block_predecessor_pending` in the no-earlier-history geometry
- **WHEN** `emit_predecessor_candidates` runs against the real §8 gate
- **THEN** the emitted predecessor is itself blocked with the same typed
  reason and `operator_action_required=True`, demonstrating the gap does not
  self-heal without operator backfill

#### Scenario: emitted predecessor is admitted under a real §8 gate in the self-heal geometry

- **GIVEN** `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true` and a successor
  blocked with the same typed reason whose state index holds the emitted
  predecessor's own verified warm-start state (`self_heal_expected=True`,
  `self_heal_probe.ready=True`)
- **WHEN** `emit_predecessor_candidates` runs against the real §8 gate
- **THEN** the emitted predecessor is ADMITTED (present in the candidate
  list, absent from blocked) and the successor's emission record carries
  `status="emitted"` — the "stand down" half of the signal is backed by the
  machinery, not only by field values

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

A forecast cohort reservation whose basin carries the `retry_journal_predecessor_identity_mismatch` decision SHALL stamp quarantine-rerun provenance onto the cohort MASTER row (the affected model ids, written by the reservation writer — the §8.7 scoring and filtering surfaces themselves remain strictly read-only). When the stale recorded `init_state_id` for one cycle and model has been re-recorded by at least one qualifying cohort MASTER row carrying that provenance for that model — counted read-only from the journal, never from the bounded candidate-state payload — the scheduler SHALL treat that row as a completed convergence attempt when either the master has aggregate terminal-success status or the master has `partially_failed` status and its bounded `candidate_projections` contains the exact model with `array_task_outcome="succeeded"`. An aggregate terminal-success master SHALL remain countable without `candidate_projections`; a `partially_failed` master whose exact model projection is failed, missing, malformed, or unavailable after the 256-entry bound SHALL NOT count. Per-model terminal rows that reconcile copies the identity onto are excluded, so one submission's master row plus its per-model terminal row count as one. Masters minted by non-quarantine replacements (for example a missing-run-manifest or missing-forecast-output resubmission) carry no such provenance and SHALL NOT arm the breaker; journals written before this change carry no provenance field and SHALL leave the breaker disengaged. Once the count reaches the existing threshold, the scheduler SHALL stop producing the quarantine retry: the candidate-side filter SHALL demote the decision to a blocked decision carrying a typed reason, the recorded and expected identity tokens, the occurrence count, and a manual-retry-required retry policy; the discovery-side backfill selection SHALL exclude a cycle from the single oldest-first execution slot only when every model keeping that cycle a gap is breaker-engaged (a cycle with any genuinely incomplete model SHALL keep taking the slot), while still reporting the excluded cycle as a gap (never as complete) and emitting a not-selected evidence entry that carries both identity tokens. No journal row SHALL be written or deleted, and an unavailable or failed occurrence count SHALL leave the breaker disengaged and the quarantine retry decision unchanged (fail toward liveness).

#### Scenario: Breaker demotes the quarantine to blocked after a provenance-stamped rerun re-records the token

- **WHEN** a completed quarantine rerun (its master row stamped with quarantine-rerun provenance for the model) has re-recorded the same stale `init_state_id` that the current positive mismatch observes
- **THEN** the candidate-side decision is the blocked decision with the recorded token, the expected token, the provenance-stamped occurrence count, and `manual_retry_required` true — while before any stamped rerun completes the decision stays the quarantine retry

#### Scenario: Partially failed cohort counts the quarantined model that succeeded

- **WHEN** a provenance-stamped master is `partially_failed`, records the stale token for the target model, and its bounded projection for that exact model has `array_task_outcome="succeeded"`
- **THEN** the occurrence count includes that master exactly once, regardless of another cohort member's failed task

#### Scenario: Partially failed cohort does not count a failed or unavailable target model

- **WHEN** a provenance-stamped master is `partially_failed` but the target model's projection says `failed`, is absent, is malformed, or is unavailable after bounded projection truncation
- **THEN** the master contributes zero occurrences for that model and the breaker remains fail-toward-liveness

#### Scenario: Aggregate-success legacy shape remains countable without projections

- **WHEN** a provenance-stamped aggregate terminal-success master records the stale token but has no `candidate_projections` field
- **THEN** it contributes one occurrence exactly as before this change

#### Scenario: Non-quarantine replacements do not pre-arm the breaker

- **WHEN** the journal holds two or more qualifying masters recording the same stale token but none carries quarantine-rerun provenance for the model (for example the second master came from a missing-run-manifest or missing-forecast-output replacement)
- **THEN** the first quarantine judgement is the ordinary quarantine retry, and the convergence layer gets its rerun before any fail-stop

#### Scenario: Pre-change journals leave the breaker disengaged

- **WHEN** the cycle's journal rows were written before this change and carry no provenance field
- **THEN** the breaker stays disengaged and the quarantine retry behavior is unchanged

#### Scenario: Breaker-engaged gap releases the backfill execution slot

- **WHEN** the oldest available incomplete cycle is breaker-engaged and a later available gap exists for the same source
- **THEN** the later gap is selected for execution, the breaker-engaged cycle appears as a not-selected evidence entry carrying both identity tokens, and its completion status remains gap

#### Scenario: One submission's master and terminal rows count as one

- **WHEN** a single qualifying provenance-stamped quarantine rerun has written the stale token onto its cohort master row and reconcile has copied the identity onto the model's per-model terminal row
- **THEN** the provenance-stamped occurrence count for that token is exactly 1 — the reconcile copy is never double-counted — and, that count witnessing one failed convergence attempt, the breaker engages; the same two rows without the provenance stamp count 0 and leave the quarantine retry unchanged

#### Scenario: Mixed cycle with a genuinely incomplete model keeps the slot

- **WHEN** one model of cycle T is breaker-engaged while another model of the same cycle still has work the scheduler could progress — whether it has no completed pipeline at all, or has its own positive identity mismatch whose provenance-stamped count has not engaged the breaker
- **THEN** cycle T still takes the backfill execution slot and executes normally for the model that is not breaker-engaged

#### Scenario: Unavailable occurrence count leaves the breaker disengaged

- **WHEN** the journal occurrence count cannot be read (no accessor on the repository, no rows, or unreadable rows)
- **THEN** the breaker stays disengaged and the candidate-side decision remains `retry_journal_predecessor_identity_mismatch`

#### Scenario: Journal remains untouched by the breaker

- **WHEN** the breaker engages for cycle T
- **THEN** the journal's on-disk content is byte-identical after the pass

### Requirement: Copyback root sameness SHALL be decided by filesystem identity rather than resolved-path string equality

The state-index copyback replay tool and the natural run-tree copyback path SHALL decide "reference root and destination root are the same storage location" by comparing the `(st_dev, st_ino)` identity of the two directories, obtained through a single shared no-follow descriptor probe, instead of comparing their resolved path strings.

Both sites SHALL keep their existing structured outcome for the same-root case — the replay tool raises `roots_identical` and the run-tree path returns its `copyback_root_matches_object_store_root` skip — and SHALL NOT introduce a new error code or change the payload fields.

Overlap (parent/child containment) detection SHALL remain resolved-path string comparison, because inode identity cannot express containment; consequently an aliased parent/child overlap remains undetectable, which is an accepted limit of this change.

The identity comparison SHALL be evaluated before the overlap comparison at both sites, preserving today's ordering, and a failure of the identity probe SHALL surface through each site's existing root-unavailability error rather than being swallowed or degraded into a permissive pass.

#### Scenario: Same-inode alias roots are refused by the replay tool instead of deadlocking

- **GIVEN** the replay tool is invoked with a reference root and a destination root whose resolved path strings differ
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the root-conflict guard runs
- **THEN** the tool fails closed with the structured reason `roots_identical` and a non-zero exit
- **AND** no state index is written at the destination and no partial receipt is produced
- **AND** the call returns within a bounded timeout rather than blocking on the provider destination lock

#### Scenario: Same-inode alias roots take the run-tree copyback skip branch

- **GIVEN** `copyback_run_trees` is called with an object-store root and a copyback root whose resolved path strings differ, and with the state-index object key among its extra object keys
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the same-root guard runs
- **THEN** the call returns the existing skip result with reason `copyback_root_matches_object_store_root`
- **AND** `merge_state_snapshot_index_copyback` is never reached
- **AND** the call returns within a bounded timeout rather than blocking on the provider destination lock

#### Scenario: Genuinely distinct roots and symlink aliases keep their current behavior

- **GIVEN** a reference root and a destination root that are two distinct directories with distinct filesystem identities
- **WHEN** the root-conflict guard runs
- **THEN** the copyback proceeds exactly as before the change, provided every ancestor directory of both roots is readable
- **AND** when the destination root is instead a symlink alias of the reference root, resolution still collapses it and it is still refused as the same root
- **AND** when one root is a strict subdirectory of the other, the existing `roots_overlap` refusal still fires

### Requirement: A single shared no-follow directory-identity probe SHALL be the only source of root identity for the guards this change touches

A helper `directory_identity_no_follow` SHALL exist in the shared safe-filesystem module, returning the `(st_dev, st_ino)` pair of an existing directory opened through the module's existing per-component `O_NOFOLLOW` descriptor walk followed by `os.fstat`, and each copyback same-root guard changed by this change SHALL obtain root identity only through that helper.

The guards in question are exactly: the state-index copyback replay root-conflict guard, the run-tree copyback same-root branch, the tile-publisher run-product and q_down copyback same-root branches, and the forcing-copyback backfill same-root rejection. No claim is made about guards outside this enumeration.

The helper SHALL NOT introduce any path-following stat call, and SHALL close the descriptor it opens on every exit path. Because the descriptor walk rejects every symlink component including the final one, the helper SHALL be called only on already-resolved paths.

The identity claim carried by this helper is limited to aliases that share a filesystem superblock; aliases that present the same object under different `st_dev` values are outside what this comparison can detect.

#### Scenario: The probe reports identity, not the input string

- **GIVEN** one directory addressed through two different input strings that expand to the same resolved path
- **WHEN** the probe is called on each string
- **THEN** both calls return the same `(st_dev, st_ino)` pair
- **AND** the probe called on a second, genuinely distinct directory returns a different pair

#### Scenario: The probe refuses symlink components instead of following them

- **GIVEN** a path whose final component is a symlink pointing at a directory
- **WHEN** the probe is called on it
- **THEN** the call raises rather than returning the identity of the link target

### Requirement: The state-index copyback merge SHALL refuse before acquiring any lock when both provider lockfiles are one file

The copyback merge SHALL determine, before acquiring the source provider destination lock, whether the source and destination provider lockfiles denote the same file, and SHALL fail closed with a structured state-manager error when they do, rather than blocking forever on a non-reentrant lock it already holds.

Sameness SHALL be decided by two complementary tests, either of which is sufficient. The first compares the `(st_dev, st_ino)` identity of the two index files' parent directories together with the two lockfile basenames, and therefore holds whether or not the lockfiles have been created yet. The second compares the `(st_dev, st_ino)` identity of the lockfiles themselves and applies only when both already exist, catching hardlinked lockfiles in genuinely distinct directories that the first test cannot see.

A path that does not exist SHALL make its test inapplicable rather than raise, on either side and for either test, because a nonexistent directory or lockfile has no inode and cannot alias an existing one. This explicitly covers the bootstrap copyback, where the destination index's parent directory is created by the lock acquisition itself and therefore does not exist when the guard runs; that merge SHALL continue to succeed exactly as it does today. Every other probe failure SHALL fail closed with the same structured error rather than being swallowed or degraded into a permissive pass.

#### Scenario: Hardlinked lockfiles in distinct directories are refused instead of self-deadlocking

- **GIVEN** a source index and a destination index in two genuinely distinct directories
- **AND** their two provider lockfiles are hardlinked to one inode
- **WHEN** the copyback merge is invoked
- **THEN** it fails closed with a structured state-manager error naming the lockfile collision
- **AND** it returns within a bounded timeout instead of blocking on the second lock acquisition
- **AND** no provider lock is acquired and neither index is modified

#### Scenario: Aliased parent directories are refused before the lockfiles exist

- **GIVEN** a source index and a destination index whose parent directories report the same `(st_dev, st_ino)` identity
- **AND** neither provider lockfile has been created yet
- **WHEN** the copyback merge is invoked
- **THEN** it fails closed with the same structured error
- **AND** the refusal happens before any lock acquisition

#### Scenario: The bootstrap copyback is not refused

- **GIVEN** a destination index whose parent directory does not exist yet, as on the first copyback into a fresh copyback root
- **WHEN** the copyback merge is invoked
- **THEN** the guard treats the absent path as an inapplicable test rather than a probe failure
- **AND** the merge proceeds and creates the destination index exactly as it does today

#### Scenario: Genuinely distinct lockfiles keep today's behavior

- **GIVEN** a source index and a destination index whose parent directories have distinct identities and whose lockfiles, if present, are distinct inodes
- **WHEN** the copyback merge is invoked
- **THEN** the merge proceeds exactly as before this change
- **AND** the outer lock is still acquired with its existing blocking semantics

### Requirement: Both copyback callers SHALL classify the pre-lock refusal as failed-closed rather than commit-uncertain

The refusal and any probe-failure wrap SHALL be raised as structured state-index errors carrying a named reason and carrying no commit phase, and both production copyback callers SHALL report them as pre-commit failures, because nothing has been read, written, or locked when the guard fires.

The run-tree copyback caller classifies by phase, so a phase-free error already lands in its failed-closed bucket. The replay tool classifies by a reason allowlist instead, so every reason this guard can raise SHALL be added to that allowlist; otherwise the replay tool would report an untouched shared index as possibly committed, run its committed-tail verification, and write an uncertain receipt.

The evidence carried by these errors SHALL use the module's existing state-index evidence treatment for local paths rather than embedding raw absolute paths in the error message.

#### Scenario: A pre-lock refusal is classified failed-closed by the run-tree caller

- **GIVEN** the lockfile-identity guard refuses a merge
- **WHEN** the run-tree copyback caller classifies the raised error
- **THEN** it reports the failed-closed state-index code
- **AND** it does not report the commit-uncertain code

#### Scenario: A pre-lock refusal is classified as a refusal by the replay tool

- **GIVEN** the lockfile-identity guard refuses a merge invoked through the replay tool
- **WHEN** the replay tool classifies the raised error
- **THEN** it reports a refusal rather than an uncertain commit state
- **AND** it does not run the committed-destination verification
- **AND** it does not write a receipt claiming an uncertain commit state

