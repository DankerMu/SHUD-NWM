## MODIFIED Requirements

### Requirement: Cycle-granularity manual retry markers require model attribution and cycle-scope markers cannot pin candidate attempts

A cycle-granularity manual retry marker SHALL be adopted by a model
candidate only with explicit model attribution: events of
`entity_type` `forecast_cycle` whose `details` (or event top level)
carry no `model_id` matching the candidate are not adopted by any
model candidate (fail-closed); an explicit matching `model_id` makes
exactly the named candidate adopt it. All other manual retry markers
— job-targeted events, events without an entity reference, and
cycle-scope job events — keep their existing adoption semantics, so
operator manual retries of cycle-level stages remain effective for
the cycle's candidates. Separately, the `retry_count` of a marker
that resolves to a cycle-scope pipeline job — a model-less job row
whose `run_id` carries the `cycle_<source>_<stamp>` grammar (a
model-less row with a candidate-run `fcst_...` id is NOT
cycle-scope) — SHALL pin the derived attempt only when that cycle
stage's failure is the repair target: the resolved job is still a
LIVE failure AND either the state's failed stage equals the
resolved job's stage or the candidate has no live candidate-scoped
failure of its own. The marker-target test and the candidate-scope
scan read the SAME row-level live-failure domain (the row-absent
arm for unresolvable marker entities reads the target row's
write-time shape off the MARKER'S OWN RECORD when the marker
carries it, reconstructing the target and running the same
resolved-row ROUTING over the reconstruction — a model-bearing
record short-circuits to a pin exactly as the router does, a
model-less record runs this row-level domain; only markers
written without that record fall back to state-level staleness
evidence alone, and only the target's POST-WRITE fate outside the
two state mappings remains a disclosed divergence — see the
record-borne scenario below): a status in the failure half of the
module's blocker STATUS domain — the failed-pipeline statuses plus
`cancelled`, a `cancelled` row being a first-class manual-retry
repair target on the marker side exactly as it is a live failure
on the candidate side — excluding ACTIVE statuses, with repaired
stage-evidence rows and unsubmitted auto-retry placeholders never
counting; the marker-target test and the candidate-scope scan
derive from one shared row predicate so the two sides cannot
drift. The candidate-side live-failure domain matches that same
failure half of the module's blocker STATUS domain, not the
narrower failed-pipeline status set alone — read from candidate-scope job
rows and the candidate's own hydro run only (the blocker scan's
state-level `pipeline_status` and pipeline-event sources are not
live-failure sources here: a top-level failed `pipeline_status`
records the cycle failure being repaired, and counting it would
make the only-failure-left arm unreachable; on the production read
paths this exclusion is enforced by projection shape — a surviving
marker proves real job rows exist beside it — rather than by an
in-module filter, and hardening the module against synthesized
job-row-less shapes is tracked as #1299): a
candidate-scope (non-cycle-scope) job row in a failed or
`cancelled` status counts, and so does a hydro run whose status is
`failed`, `cancelled`, or `permanently_failed`; repaired
stage-evidence rows and unsubmitted auto-retry placeholders (rows
whose status is `pending` or `submission_failed` by that
placeholder's own definition) never count as live failures — a
placeholder-shaped row in a `cancelled` status is outside the
placeholder gate and counts, exactly as the blocker scan treats
it. In every other case — a candidate-scoped live failure
(pipeline failed or cancelled, or hydro) where the failed stage
does not name the resolved job's stage, or a marker whose resolved
job is no longer a live failure (stale — resolved/succeeded, still
ACTIVE, repaired stage evidence, or an unsubmitted auto-retry
placeholder; NOT a `cancelled` row, which stays a valid marker
target) — the derived
`new_attempt` falls back to
the candidate's own `previous_attempt + 1`, and the
attempt-derivation scan is terminal at the newest adopted marker:
absent a state-level manual-retry attempt payload (a top-level
`manual_retry` — or, by the same gate, `manual_retry_marker` —
mapping's `new_attempt`/`retry_count` short-circuits ahead of the
event scan; its semantics are outside this rule and unchanged by
it), that marker alone decides, whether or not it
carries a `retry_count` — a newest adopted marker whose
`retry_count` is absent or empty makes no operator attempt claim
and SHALL yield the same fallback instead of a walk-back to any
older marker's `retry_count` — so older adopted markers are never
consulted, while a newer marker-shaped event that is NOT adopted
by the candidate neither decides nor terminates the scan. Neither a refused pin
nor an absent attempt claim re-mints a
consumed attempt number: whenever the fallback's attempt derivation
resolves no canonical failed stage, it floors `previous_attempt` at
the candidate's own stage-scoped attempt record for each stage in
the restarted-stage family — the stages of the candidate's own live
candidate-scope failures (a row the live-failure exclusions above
exclude contributes no stage to the family) plus the canonical
forecast stage when the hydro run is the live failure. Within a
family stage that is itself a canonical downstream restart stage
the floor uses the same stage-scoped derivation the
resolved-stage path uses, counting id retry suffix or recorded
retry count regardless of row status — a repaired `_retry_3` row at
a family stage still proves attempt 3 was spent — while a consumed
suffix at a stage outside the family (a cross-stage forcing row or
a cohort stage counter) never contributes to this floor (the floor
only raises the caller's value). In that unnameable-stage case a
candidate whose visible family-stage row already consumed attempt
N derives at least N + 1 whenever that family stage is itself a
canonical downstream restart stage; at a non-canonical family
stage the derivation degenerates to the candidate's flat record
and the floor adds nothing (tracked as #1298); with no live
failure at all the fallback stays `previous_attempt + 1`. Marker-shaped events remain excluded from
blocker scanning regardless of attribution (a foreign marker must
never be treated as an active blocker suppressing the candidate's
own manual retry), and candidate-state event-row visibility on the
journal/DB read paths is unchanged for cycle-granularity markers and
model-less cycle-scope rows — those cycle-wide events stay visible in
every candidate's raw state for diagnostics. Candidate-state
membership for pipeline job rows on the journal (db-free) read path
SHALL align with the DB read path's candidate-state predicate: a row
whose run id is the candidate's own run id belongs to the candidate;
a row whose run id carries the cycle-scope run grammar belongs to the
candidate only when its `model_id` is empty (the model-less cohort
contract — such rows stay visible to every candidate in the cycle,
including the journal-only widening to rows whose run id extends the
cycle run id with a suffix, which this rule leaves in place) or names
the candidate itself; a row naming a foreign `model_id` is excluded
from the candidate's job rows, and a `pipeline_job`-entity event
resolving to an excluded row SHALL leave the candidate's event table
in the same filtering step as its row — an orphaned marker whose row
was excluded but whose event survived would re-enter the pinning
decision through the unresolvable cycle-scope entity grammar — so a
foreign model's manual retry marker can neither report
`manual_retry_requested` nor pin the candidate's derived
`new_attempt`. The candidate-state membership exclusion and the
cycle-level gates draw different lines: the duplicate-submission
gates (the active-pipeline and active-slurm-jobs scans) keep their
wider unconditional cycle-run visibility unchanged — the DB read
path's counterparts deliberately share that wider visibility — but
the completed-pipeline gate answers a candidate-scoped question
("has THIS candidate completed") and SHALL NOT count a
foreign-model named cycle-run row as the candidate's completion:
its job-row conjunction excludes a row whose `model_id` is
non-empty and names another model while its run id is exactly the
cycle run id, so completion is proven only by the candidate's own
rows (its own run id or its own `model_id`), by model-less
cycle-scope cohort completion rows (which stay cycle-wide — every
candidate completes through them), or by the candidate's own
completed hydro run. This aligns the journal verdict's direction
with the DB completed-pipeline gate, which reads `hydro.hydro_run`
under a source/cycle/model three-key restriction and never sees
another model's job rows; the exclusion lives in the
completed-pipeline gate's own conjunction, not in the shared
row-match predicate that feeds the duplicate-submission gates. On the
identity-filtered
decision state, preserving the attribution predicate fields makes a
self-declared MATCHING `model_id` a retention credential for a
non-authoritative marker event under shared-cycle scoping (foreign
model ids stay excluded; within one source-cycle aggregate a model id
maps to exactly one candidate), so a candidate-own marker that
sanitization previously stripped to anonymity is now retained and can
drive the retry decision it was written to request.

#### Scenario: Foreign-model named cycle-run_id row cannot enter the candidate state or pin its attempt

- **WHEN** on the journal (db-free) read path another model's
  pipeline job row is recorded with `run_id` equal to the cycle run
  id (`cycle_<source>_<stamp>`) and a non-empty `model_id` naming
  that other model, carrying `retry_count` 5, a manual retry event of
  `entity_type` `pipeline_job` targets exactly that row, and the
  candidate's own failed forecast row carries `retry_count` 0
- **THEN** the candidate's state contains neither the foreign model's
  job row nor the event targeting it, `manual_retry_requested` stays
  false from that marker, and the derived `new_attempt` is
  `previous_attempt + 1` (1 from 0) — not the foreign marker's 5
- **AND** a model-less row with the same cycle-scope run id, or with
  a run id extending it by a suffix, remains visible to every
  candidate in the cycle
- **AND** the candidate's own row with `run_id` equal to the cycle
  run id and the candidate's own `model_id` remains visible, and a
  marker targeting it keeps its adoption and pinning semantics
- **AND** the DB read path gives the same candidate-state membership
  verdict for these rows — the foreign-model named row is excluded
  there by the `model_id IS NULL` guard on its cycle-run clause and
  the candidate's own named row is included by its model clause —
  while the suffix-extended model-less row remains a journal-only
  widening that this change leaves in place
- **AND** with the foreign-model row and its marker in place, the
  cycle-level duplicate-submission gates (active-pipeline,
  active-slurm-jobs) answer exactly as before the exclusion — the
  row stays visible to those scans — while the completed-pipeline
  gate applies its own candidate-scoped conjunction (see the
  completion-gate scenario below)

#### Scenario: Foreign-model named cycle-run_id completion row does not complete the candidate

- **WHEN** on the journal (db-free) read path another model's
  pipeline job row is recorded with `run_id` equal to the cycle run
  id (`cycle_<source>_<stamp>`), a non-empty `model_id` naming that
  other model, `status` `succeeded`, and a completion stage —
  `state_save_qc`, `publish`, or `parse` under the default terminal
  contract, or `state_save_qc` under the production
  `forecast_state_save_qc` terminal contract
- **THEN** `has_completed_pipeline` answers `False` for every other
  candidate of the cycle in all of those stage/contract
  combinations — another model's completion is never this
  candidate's completion
- **AND** when the candidate's own hydro run is recorded with
  `status` `failed`, `cancelled`, or `created` and the candidate's
  own forecast row is `failed`, the foreign completion row still
  cannot flip the candidate's verdict to `True`
- **AND** a model-less cycle-scope cohort completion row (`run_id`
  equal to the cycle run id or extending it with a suffix, empty
  `model_id`) keeps answering `True` for every candidate of the
  cycle whenever its stage is a terminal completion stage under the
  active contract, and the candidate's own completion evidence
  keeps answering `True` on the same terms — its own named
  cycle-run row and its own-run-id rows at a terminal completion
  stage under the active contract, and its own completed hydro run
  under the default contract (the production
  `forecast_state_save_qc` contract derives completion from
  pipeline job rows alone and never consults the hydro completion
  arm)
- **AND** on the same fixture the active-pipeline and
  active-slurm-jobs answers are byte-for-byte unchanged, proving
  the shared row-match predicate was not narrowed
- **AND** the DB read path already answers `False` for the foreign
  shape — its completed-pipeline gate reads `hydro.hydro_run` under
  the source/cycle/model three-key restriction — so the journal and
  DB verdicts now agree in direction for this shape instead of
  diverging

#### Scenario: Unattributed cycle-granularity marker is fail-closed with an explicit escape

- **WHEN** a manual retry event of `entity_type` `forecast_cycle`
  exists in a cycle shared by several model candidates and carries
  no `model_id` in its `details` or at the event top level
- **THEN** no candidate reports `manual_retry_requested` from that
  marker
- **AND** if the same event explicitly names one candidate's
  `model_id` and that candidate's state carries at least one
  model-scoped job row (the derived model set is non-empty), exactly
  that candidate adopts it
- **AND** the gate holds on the identity-filtered decision state:
  event sanitization preserves the attribution predicate fields
  (`entity_type`, top-level and details `model_id`) so the
  fail-closed test and its explicit escape behave identically on the
  raw and filtered state
- **AND** the preserved `model_id` doubles as a shared-cycle
  retention credential on the decision state: a non-authoritative
  marker event self-declaring the candidate's own `model_id` is
  retained (and may flip the candidate's decision from a terminal
  guard to the requested retry), while one declaring a foreign
  `model_id` — or none — is dropped as before

#### Scenario: Cycle-scope job marker pins only when its stage is the repair target

- **WHEN** a manual retry event targets a cycle-scope pipeline job
  (`model_id` empty and `run_id` in the `cycle_<source>_<stamp>`
  grammar) that is still a live failure — a failed-pipeline or
  `cancelled` status, not ACTIVE, and not a repaired stage-evidence
  row or unsubmitted auto-retry placeholder — and that cycle stage's failure
  is what the candidate decision repairs — the failed stage matches
  the job's stage, or the candidate has no live candidate-scoped
  failure of its own (the production cohort-download shape)
- **THEN** the derived `new_attempt` pins the marker's
  `retry_count`, so the operator's cycle-level manual retry stays
  effective and the minted retry identity does not reuse a consumed
  attempt number
- **AND** a `cancelled` cycle-scope marker target pins exactly as a
  failed one does: with the marker's `retry_count` 5 and the
  candidate's `previous_attempt` 0, both the same-stage arm (failed
  stage `download` beside the candidate's own failed forecast) and
  the only-failure-left arm (no failed stage, own jobs all
  succeeded) derive `new_attempt` 5, and the manual-retry payload
  carries `new_attempt` 5; this holds even when the `cancelled`
  target row is placeholder-SHAPED (a retry-suffixed id with no
  Slurm id) — the placeholder gate is status-bound to
  `pending`/`submission_failed`, so a cancelled or failed
  placeholder-shaped row is outside the gate and stays a valid
  pinning marker target, exactly as the candidate-side scan counts
  it
- **AND** the candidate-state projection SHALL produce the repaired
  annotations (`repair_status`/`active_blocker`) and
  `repaired_stage_evidence` over that same repair-target status
  domain — the failed-pipeline statuses plus `cancelled` — so the
  stale-target refusal above is producible for every status the
  marker-target test reads: a `cancelled` row repaired by a later
  succeeded retry carries the annotations exactly as a failed one
  does, and every projection surface the widened domain makes
  reachable for `cancelled` rows behaves exactly as its `failed`
  twin already did (cancelled↔failed parity), including the
  active-failure exposure of an unrepaired cancelled cycle row and
  the evidence-selection paths a repaired cancelled row enters
- **AND** when the candidate's own live failure is at a different
  stage, or the marker's resolved job is no longer a live failure
  (stale — resolved/succeeded, ACTIVE, repaired stage evidence, or
  an unsubmitted auto-retry placeholder),
  the derived `new_attempt` falls back to `previous_attempt + 1`;
  the pin refusal itself charges nothing, but when the candidate's
  own failed stage resolves to a canonical downstream stage the
  caller's `previous_attempt` is already that stage's stage-scoped
  derivation, so a multi-basin cohort row at that same stage is
  still counted there — pre-existing failed-stage cycle-blindness,
  unchanged by this change and tracked as #1300
- **AND** the candidate's own live failure that blocks the pin
  includes a `cancelled` model-scoped job row (a cancelled forecast
  with a cross-stage cycle-download marker of `retry_count` 5
  derives `new_attempt` 1 from `previous_attempt` 0, not 5) and a
  failed, cancelled, or permanently failed hydro run beside
  all-succeeded job rows (`previous_attempt` 2 derives 3, not 5) —
  the FAILURE half of the blocker scan's status domain only: an
  ACTIVE in-flight row (`pending`/`queued`/`submitted`/`running`)
  or an ACTIVE hydro run is not a repair target and never blocks
  the pin
- **AND** the refused pin's fallback floor comes from the durable
  record of the restarted stage family whenever no canonical failed
  stage resolves: a cancelled own forecast row whose job id carries
  the consumed `_retry_2` suffix (master `retry_count` reset to 0
  by the journal's clean-reservation invariant, no usable
  `failed_stage`) derives `new_attempt` 3 — not 1 (a replay of a
  consumed identity that would silently skip submission at the
  reservation boundary) and not the marker's 5 — and a consumed
  suffix at a stage outside the family (an own forcing `_retry_7`
  row, or a single-basin cohort `download`/`convert` counter)
  leaves that derivation untouched, while the emitted
  `previous_attempt` evidence fields keep reporting the unfloored
  stage-scoped derivation (only the derived `new_attempt` carries
  the floor)
- **AND** a repaired stage-evidence row or an unsubmitted
  auto-retry placeholder is not a live failure and does not block
  the pin, while a placeholder-shaped row in a `cancelled` status
  falls outside the placeholder gate and blocks the pin exactly as
  it blocks the blocker scan (same domain, same exclusions)
- **AND** the fallback is terminal even when the candidate has an
  older own-model marker: the stale marker's `retry_count` does not
  leak into `new_attempt`
- **AND** a model-less job row carrying a candidate-run `fcst_...`
  id is not cycle-scope — a marker targeting it keeps pinning
  `new_attempt` to its `retry_count`
- **AND** the rule survives candidate-state filtering with
  equivalent evidence: a marker whose entity cannot be resolved to
  any job row but whose entity id carries the cycle-scope
  pipeline-job grammar (`job_cycle_<source>_<stamp>_...`, the shape
  left behind when a non-authoritative cohort master row is dropped
  from the decision state or truncated from the row window) and
  that does NOT carry its target's write-time record (a marker
  WITH the record decides through the record-borne routing in its
  own scenario below) pins the
  candidate's attempt exactly when the id's cycle is the candidate's
  own cycle AND the marker's recorded stage is the repair target —
  the stage evidence is the marker's own `failed_stage` detail,
  with the id's stage token (read after stripping every stacked
  `_retry_<n>` suffix) as the backstop for markers written before
  the detail existed — AND neither the state-level repaired-stage
  evidence (its original failed job id) nor the state-level
  completed-stage evidence (its job id) names the marker's target
  (exact id comparisons — the staleness refusals delivered with the
  evidence the row-absent path actually has); a surviving marker
  whose stage is NOT the repair target falls through to the
  only-failure-left arm (the same widened live-failure domain, so a
  cancelled own row or a failed hydro run blocks this pin too)
  instead of refusing outright — so an operator's manual retry of
  the candidate's own cohort cycle stage stays effective even
  though the row is invisible and even on a retry-suffixed id,
  while a foreign-cycle counter or a stale repaired target still
  never pins the candidate's attempt; markers with other
  unresolvable entity ids keep their existing pinning behavior

#### Scenario: Unresolvable cycle-grammar marker pins with marker-record evidence

- **WHEN** a manual retry marker's entity cannot be resolved to any
  job row, its entity id carries the cycle-scope pipeline-job
  grammar with one or more stacked `_retry_<n>` suffixes
  (`job_cycle_<source>_<stamp>_<stage>_retry_1`, or the three-layer
  production shape `..._retry_1_retry_2_retry_3`), the id's cycle
  is the candidate's own, and the state's failed stage equals
  `<stage>`
- **THEN** the pin holds through BOTH row-absence mechanisms
  (identity-filter cohort deletion, and row-window truncation past
  a newer same-stage row) — stacked suffixes do not defeat the
  stage evidence, whether it comes from the marker's recorded
  `failed_stage` detail or from the loop-stripped id token backstop
- **AND** every cross-arm equivalence claim in this scenario reads
  within the DELIVERED DOMAIN, which is now split by what the
  marker itself recorded: a marker carrying its target's write-time
  record (the `target_*` details below) is decided by
  reconstructing the target from that record and running the SAME
  resolved-row routing over the reconstruction — a model-bearing
  target pins unconditionally exactly as the resolved-row router
  does, and a model-less target answers through the cycle-scope pin
  rule's shared row-level live-failure domain (placeholder and
  repaired-flag exclusions included) — so on everything the record
  captures the two arms are the same rule by construction; a marker
  WITHOUT the record keeps the previous delivered domain (failed-
  status targets that are neither unsubmitted auto-retry
  placeholders nor repaired-flagged, or targets a state mapping
  names with an exact entity-id match); the divergence classes
  left OUTSIDE both are the target's POST-WRITE fate beyond the
  two state mappings and the write-time shapes the record cannot
  carry (the projection-annotation keys the current writer never
  produces, #1482), enumerated as a permanent limitation below
- **AND** the journal marker event written by a manual repair
  carries the failed job's stage as a `failed_stage` detail AND the
  target row's write-time shape as `target_status`,
  `target_repair_status`, `target_active_blocker`,
  `target_model_id`, `target_slurm_job_id`, `target_retry_count`,
  `target_manual_retry_marker`, and `target_array_task_id` details
  — a key set that closes over EVERY row field the shared
  live-failure predicate's transitive closure reads (the
  placeholder predicate alone reads six; `target_repair_status`
  and `target_active_blocker` are gate-contract keys the CURRENT
  writer never fills — those flags are projection-time annotations
  absent from the persisted rows it reads, #1482), with key names
  chosen to
  avoid the candidate-state record-stage reader's
  `stage`/`job_type` keys and the attribution reader's `model_id`
  key (the target's model is a different semantic axis from the
  marker's attributed model), zero and false being recorded values
  rather than absences — all preserved by the
  identity-filter event sanitizer on retry events — so markers
  written from now on decide by record rather than id text
  wherever their details survive to adoption (on the journal read
  path the completion-stage compaction drops those details
  wholesale for model-less cycle-scope queue events at the
  completion stages, which un-adopts the marker event entirely
  rather than falling back to id text — the pin gate's journal-path
  live domain is the submission stages)
- **AND** for a model-less target, a marker whose target the
  state-level repaired-stage evidence names as its original failed
  job — or whose target the state-level completed-stage evidence
  names as its completed job — refuses the pin with the row absent
  exactly as the resolved-row rule refuses it with the row present
  (a model-bearing record short-circuits past both mappings,
  exactly as the resolved-row router does); those two mappings,
  plus the marker's own write-time record, are the row-absent
  staleness surfaces — and the target's POST-WRITE fate outside
  the two mappings is a PERMANENT LIMITATION, disclosed rather
  than delivered: a target that succeeded after the marker was
  written and was evicted from the completed-stage evidence by a
  later-stage winner, or whose success projected through the
  repaired-copy branch (no `job_id` key), or whose stage has no
  successor in the forecast stage order (`download`,
  `state_save_qc`, `publish` queue targets), or that was repaired
  after write without the repaired-stage evidence naming it, or
  that was already ANNOTATED repaired at write time (the
  projection-time annotation never reaches the persisted rows the
  writer reads), still
  pins here where the resolved-row rule would refuse — the
  completed-stage evidence producer is not widened to those stages
  because that mapping also drives restart routing; a target
  re-activated after write (resubmitted out of a non-terminal
  failure status back into the ACTIVE domain) belongs to the same
  limitation wherever that transition is producible
- **AND** with the marker's stage differing from the candidate's
  failed stage, a marker WITHOUT the record falls through to the
  only-failure-left arm — the same arm the resolved-row rule uses
  on a stage mismatch — and, within the delivered domain, for a
  failed-status target lands on the same verdict as the
  resolved-row rule on the same state (a model-bearing record
  pins on the stage mismatch itself, router parity)
- **AND** markers with non-cycle-grammar entity ids keep the
  historical fail-open, a foreign-cycle id still never pins, and a
  stage-less marker keeps deciding through the loop-stripped id
  token — a TEXT inference, not recorded evidence, capped to the
  legacy set of markers written before the `failed_stage` detail
  existed PLUS the half records the current writer still produces
  when the target row carries no stage (the empty value is not
  written and the sanitizer does not pass empties through): the
  token's stage may not be the stage the target row actually
  carried, and that ceiling is pinned as accepted behavior, not
  closed

#### Scenario: Record-borne target evidence gives the row-absent arm resolved-row parity

- **WHEN** a manual retry marker written by `record_manual_repair`
  carries its target's write-time record (`target_status` and the
  marker's `failed_stage` detail both present — a half record
  missing either falls back to the delivered backstop arm, id-token
  inference included; the remaining `target_*` keys present when
  the target row carried them) and the marker's target row is
  absent from the decision state (identity-filter deletion or
  row-window truncation)
- **THEN** for a MODEL-LESS record the pin verdict equals the
  resolved-row router's verdict on a row of exactly the recorded
  shape: an unsubmitted auto-retry placeholder record
  (`pending`/`submission_failed` status, `_retry_<n>` id, positive
  `target_retry_count`, no marker flag, no slurm or array id)
  refuses the pin, a repaired-flagged record
  (`target_repair_status` repaired or `target_active_blocker`
  false) refuses the pin — those two flags are projection-time
  annotations that never reach the persisted rows the
  manual-repair writer reads, so like the success values below
  this is the gate's contract on the record, not a shape the
  current writer produces; a target already annotated repaired at
  write time therefore still pins through its record, a disclosed
  permanent limitation alongside the post-write fates — and a
  record whose status is not in the
  live-failure domain (a succeeded
  `download`/`state_save_qc`/`publish` queue target included —
  no dependence on the completed-stage evidence, whose producer
  never names those stages; such success values lie outside the
  manual-repair writer's own source domain and this clause is the
  gate's contract on the record, not a claim the writer produces
  them) refuses the pin — each exactly as the row-present twin
  refuses the same shape
- **AND** a model-bearing record whose `target_model_id` names the
  candidate's OWN model — read off the tail of the state's own
  candidate run id (`fcst_<source>_<stamp>_<model_id>`, everything
  after the stamp, model ids carrying underscores of their own),
  never derived from the surviving job rows, so row-window
  truncation cannot blind the comparison — pins unconditionally,
  cross-stage and same-stage alike, even when a state staleness
  mapping names the target — exactly as the resolved-row router
  short-circuits a model-bearing row to a pin — so the
  operator-pinned `retry_count` is honored on both sides, while a
  record naming any OTHER model, or a state whose run id yields no
  model, fails closed and never pins
- **AND** the verdicts hold for stacked-suffix entity ids
  (`..._retry_1` and `..._retry_1_retry_2_retry_3` alike)
- **AND** a marker without the record — legacy markers, and every
  marker written by the SQL retry service — keeps the delivered
  backstop verdicts bit for bit
- **AND** the identity-filter event sanitizer preserves the
  `target_*` details on retry events end to end: a marker written
  by `record_manual_repair`, projected into the candidate state and
  filtered onto the decision state, still carries them at the pin
  gate

#### Scenario: Newest adopted marker without retry_count terminates the attempt scan

- **WHEN** the candidate's events contain an older adopted
  own-model marker carrying `retry_count` N whose pin would
  otherwise hold, followed by a newer adopted marker whose
  `retry_count` is absent or the empty string (a cross-stage
  cycle-granularity marker, or a marker written without the
  field), and the candidate's `previous_attempt` is N
- **THEN** the derived `new_attempt` is the fallback
  `previous_attempt + 1` — floored by the restarted-stage-family
  rule exactly as every other fallback arm — and never the older
  marker's consumed N
- **AND** the manual-retry payload reports the retry as requested
  from the newest marker and carries no `new_attempt` claim: the
  payload scan and the attempt-derivation scan terminate at the
  same newest adopted marker
- **AND** a newest adopted marker that itself carries a pinning
  `retry_count` keeps deciding with its value exactly as before,
  and a state with no adopted marker at all keeps its existing
  fallback semantics
- **AND** a marker-shaped event newer than the candidate's own
  pinning marker but NOT adopted by the candidate (for example a
  foreign-attributed marker) neither terminates the scan nor
  decides — the candidate's own newest adopted marker still pins
  its `retry_count`
- **AND** when the terminal fallback fires on the shape where no
  canonical failed stage resolves (a cancelled own row whose job id
  carries a consumed `_retry_<n>` suffix), the restarted-stage-family
  floor applies exactly as on the other fallback arms — the
  derivation returns the floored value, not a bare
  `previous_attempt + 1` replay of a consumed identity
- **AND** on a state carrying no state-level manual-retry attempt
  payload (no top-level `manual_retry` or `manual_retry_marker`
  mapping whose `new_attempt`/`retry_count` value is neither `None`
  nor `""`), absent an adopted marker whose `retry_count` pins (an
  operator's explicit attempt claim), the derivation never returns
  a value at or below `previous_attempt`

#### Scenario: Own-model markers and blocker exclusion keep their semantics

- **WHEN** a manual retry event targets one of the candidate's own
  model-scoped jobs, or a foreign marker-shaped event (e.g.
  `status_to` `pending`) coexists with the candidate's own marker
- **THEN** the own-model marker is adopted unchanged with
  `new_attempt` matching its `retry_count`, and the foreign
  marker-shaped event is not treated as an active blocker — the
  candidate's `manual_retry_requested` remains truthful

