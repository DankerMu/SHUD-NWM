# Delta: job-retry-mechanism (cycle-pin-live-failure-domain)

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
stage's failure is the repair target: the resolved job is still in
a failed status AND either the state's failed stage equals the
resolved job's stage or the candidate has no live candidate-scoped
failure of its own. The live-failure domain matches the failure
half of the module's blocker domain, not the narrower
failed-pipeline status set alone: a model-scoped job row in a
failed or `cancelled` status counts, and so does a hydro run whose
status is `failed`, `cancelled`, or `permanently_failed`; repaired
stage-evidence rows and unsubmitted auto-retry placeholders (rows
whose status is `pending` or `submission_failed` by that
placeholder's own definition) never count as live failures — a
placeholder-shaped row in a `cancelled` status is outside the
placeholder gate and counts, exactly as the blocker scan treats
it. In every other case — a
candidate-scoped live pipeline failure (failed or cancelled) at a
different stage, a candidate-scoped live hydro failure, or a marker
whose resolved job is no longer failed (stale) — the derived
`new_attempt` falls back to
the candidate's own `previous_attempt + 1`, and the fallback is
terminal: the newest retry-count-bearing adopted marker decides;
older markers are not consulted. Marker-shaped events remain excluded from
blocker scanning regardless of attribution (a foreign marker must
never be treated as an active blocker suppressing the candidate's
own manual retry), and candidate-state event-row visibility on the
journal/DB read paths is unchanged — cycle-wide events stay visible in
every candidate's raw state for diagnostics. On the identity-filtered
decision state, preserving the attribution predicate fields makes a
self-declared MATCHING `model_id` a retention credential for a
non-authoritative marker event under shared-cycle scoping (foreign
model ids stay excluded; within one source-cycle aggregate a model id
maps to exactly one candidate), so a candidate-own marker that
sanitization previously stripped to anonymity is now retained and can
drive the retry decision it was written to request.

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

- **WHEN** a manual retry event targets a still-failed cycle-scope
  pipeline job (`model_id` empty and `run_id` in the
  `cycle_<source>_<stamp>` grammar) and that cycle stage's failure
  is what the candidate decision repairs — the failed stage matches
  the job's stage, or the candidate has no live candidate-scoped
  failure of its own (the production cohort-download shape)
- **THEN** the derived `new_attempt` pins the marker's
  `retry_count`, so the operator's cycle-level manual retry stays
  effective and the minted retry identity does not reuse a consumed
  attempt number
- **AND** when the candidate's own live failure is at a different
  stage, or the marker's resolved job is no longer failed (stale),
  the derived `new_attempt` falls back to `previous_attempt + 1` —
  the cycle job's counter is never charged to the candidate's
  forecast-level budget
- **AND** the candidate's own live failure that blocks the pin
  includes a `cancelled` model-scoped job row (a cancelled forecast
  with a cross-stage cycle-download marker of `retry_count` 5
  derives `new_attempt` 1 from `previous_attempt` 0, not 5) and a
  failed, cancelled, or permanently failed hydro run beside
  all-succeeded job rows (`previous_attempt` 2 derives 3, not 5) —
  the same live-failure domain the module's blocker scan applies
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
  from the decision state or truncated from the row window) pins the
  candidate's attempt exactly when the id's cycle is the candidate's
  own cycle AND the id's stage is the repair target (the id ends
  with the state's failed stage, or with no failed stage the
  candidate has no live failure of its own — the same widened
  live-failure domain, so a cancelled own row or a failed hydro run
  blocks this pin too) — so an operator's manual retry of the
  candidate's own cohort cycle stage stays effective even though
  the row is invisible, while a foreign-cycle or cross-stage cycle
  counter still never charges the candidate's budget; markers with
  other unresolvable entity ids keep their existing pinning behavior

#### Scenario: Own-model markers and blocker exclusion keep their semantics

- **WHEN** a manual retry event targets one of the candidate's own
  model-scoped jobs, or a foreign marker-shaped event (e.g.
  `status_to` `pending`) coexists with the candidate's own marker
- **THEN** the own-model marker is adopted unchanged with
  `new_attempt` matching its `retry_count`, and the foreign
  marker-shaped event is not treated as an active blocker — the
  candidate's `manual_retry_requested` remains truthful
