## MODIFIED Requirements

### Requirement: Strict-warm-start terminal mismatch retries SHALL respect a stage-scoped budget

When the candidate ladder would emit `retry_strict_warm_start_terminal_init_state_mismatch` for a terminal-success candidate whose recorded init-state identity mismatches the strict warm-start resolution, the scheduler SHALL first evaluate the stage-scoped retry attempt against the configured retry limit. When the attempt has reached the limit, the scheduler SHALL emit the stable blocked decision `blocked_strict_warm_start_init_state_mismatch` carrying a retry-policy block (automatic retry not allowed, manual retry required, attempt, retry limit) instead of the retry decision, and the blocked decision SHALL NOT participate in forced terminal resubmission or replacement-retry scoping. When the attempt is below the limit, the retry decision and its evidence SHALL remain unchanged.

Every explicit stage-scoped attempt read SHALL derive only from rows whose authoritative stage resolves to the requested stage, plus the existing identity-narrowed carried floor for canonical downstream stages. It SHALL NOT max the result against the candidate-level flat `retry_count`: that flat aggregate remains a window-local stage-less compatibility/evidence value, and an unrelated stage's persisted count SHALL NOT charge the requested stage's budget. A stage-less read SHALL retain its flat-first behavior and SHALL NOT consume carried stage floors.

The stage-scoped attempt SHALL bind in the production geometry where the reserved master row carries no retry count and attempts are recorded only as retry-suffixed pipeline job rows — including the reverse geometry where the maximum-attempt retry-suffixed row is older than `job_limit` fresher rows of other stages. Both file-journal and DB-backed candidate-state paths SHALL provide the shared projection with the complete job population admitted by their candidate/cycle query before the projection applies `job_limit`; the projection SHALL derive each canonical downstream stage's maximum effective retry attempt through the same authoritative-stage-field and `effective_retry_attempt` chain the budget consumers use (including the `job_type` fallback for stage-less rows and the persisted-`retry_count` half for rows without a `_retry_<n>` suffix), never job-id substring parsing and never a locally forked stage-alias table, and carry these upper bounds across truncation.

The carried upper bounds SHALL record their contributing rows' candidate-identity metadata, and candidate-identity/scope filtering SHALL narrow the carried upper bounds with the row population: a stage's upper bound survives a filtered state only while at least one of its contributing rows passes the same authority/scope predicates that filter the rows — an upper bound whose every contributor is judged non-authoritative for the candidate SHALL NOT reach that candidate's failure-policy, budget, or mint derivations. The truncated row selection itself SHALL remain the pure-freshness top-`job_limit` selection, element for element identical to the pre-change projection in every geometry — the carried upper bounds SHALL add no rows, evict no rows, and change no state key derived from the returned row population. The projection SHALL report `pipeline_jobs_total` as the true number of admitted job rows before `job_limit`, and `state_truncated` SHALL reflect that true total. The DB-backed and file-journal paths SHALL therefore produce the same stage-scoped attempts for the same admitted row geometry while returning no more than `job_limit` pipeline-job rows.

#### Scenario: Budget exhaustion demotes the retry to a stable blocked decision

- **WHEN** a completed cycle's candidate has a strict warm-start init-state mismatch and its stage-scoped retry attempt has reached the retry limit
- **THEN** the decision is `blocked_strict_warm_start_init_state_mismatch` with the retry-policy block, no forecast work is selected for resubmission, and the candidate is not re-selected on subsequent passes while the mismatch persists

#### Scenario: Below-budget behavior is unchanged

- **WHEN** the same mismatch is observed while the stage-scoped attempt is below the retry limit
- **THEN** the emitted decision and evidence are byte-identical to today's `retry_strict_warm_start_terminal_init_state_mismatch` shape

#### Scenario: The blocked decision is excluded from force-resubmit whitelists

- **WHEN** orchestration evaluates forced terminal resubmission and replacement-retry scoping against a candidate carrying the blocked decision
- **THEN** neither whitelist matches — their member sets are unchanged by this change — and no replacement submission occurs

#### Scenario: The budget binds against retry-suffixed journal rows

- **WHEN** the candidate state derives from a journal containing a reserved master row with zero retry count and stage-matching `*_retry_N` pipeline job rows mirroring the production wedge geometry
- **THEN** the stage-scoped attempt evaluates to at least N and the demotion triggers once N reaches the retry limit

#### Scenario: The budget binds in the reverse truncation geometry

- **WHEN** the `*_forecast_retry_N` row carrying the stage's maximum attempt is older than `job_limit` fresher rows of other stages and `N` has reached the retry limit
- **THEN** both the file-journal and DB-backed projections still yield stage-scoped attempt `N`, and the demotion to `blocked_strict_warm_start_init_state_mismatch` triggers instead of an unbudgeted retry

#### Scenario: Projection row selection is unchanged in every geometry

- **WHEN** any input geometry is projected — including the reverse geometry, a geometry whose only completed-stage success row sits at the old end of the freshness window, a geometry with a stale active-status row outside the window, and a geometry whose flat `retry_count` carrier sits inside the window
- **THEN** `pipeline_jobs` equals the freshness-ordered top-`job_limit` selection element for element, and every state key derived from the row population (`pipeline_status`, `failed_stage`, `restart_stage`, completed-stage evidence, active-job scanning, the flat `retry_count` aggregate, `latest_job` derivation) is identical to the pre-change projection

#### Scenario: DB projection reports true population without breaking the hard returned-row bound

- **WHEN** the DB-backed candidate query admits more than `job_limit` job rows
- **THEN** `pipeline_jobs_total` equals the full admitted count, `state_truncated` is true, and the returned `pipeline_jobs` list contains exactly the pure-freshness top `job_limit` rows
- **AND** no SQL-local stage alias table or retry-suffix parser decides which attempts survive

#### Scenario: Friendly DB geometry remains byte-identical

- **WHEN** every stage's maximum-attempt contributor already lies inside the newest `job_limit` rows
- **THEN** the DB-backed projected row list and all row-derived state keys are element-for-element identical to the prior pure-freshness result

#### Scenario: Zero-attempt stages carry no upper bound

- **WHEN** a canonical stage's maximum effective attempt in the input is zero
- **THEN** the carried upper bounds contain no entry for that stage and stage-scoped derivation still returns zero

#### Scenario: Upper bounds derive through the consumer chain on degenerate row shapes

- **WHEN** the input carries an out-of-window maximum-attempt `copyback` row, out-of-window `download` rows, a row whose attempt lives only in its persisted `retry_count` (no `_retry_<n>` suffix), and a row whose stage lives only in `job_type`
- **THEN** the `copyback`, persisted-`retry_count`, and `job_type`-only upper bounds are all carried (canonical stages via the consumer chain) while `download` rows contribute no canonical upper bound, matching the consumer-side canonical-stage table rather than any locally forked alias table or job-id substring parsing

#### Scenario: Carried upper bounds narrow with identity filtering

- **WHEN** a stage's only upper-bound contributor is a row that candidate-identity filtering judges non-authoritative for the candidate (for example a model-less suffixed cohort row) and the filtered row population drops it
- **THEN** the filtered state carries no upper bound for that stage, and the candidate's own first failure classifies exactly as it did before this change — retriable, attempt zero — instead of inheriting the foreign row's attempt

#### Scenario: The strict-warm-start budget reads the narrowed upper bounds

- **WHEN** the strict-warm-start terminal-mismatch budget evaluates a candidate whose only upper-bound contributor for the forecast stage is a non-authoritative cycle-cohort row truncated out of the window with its attempt at the retry limit
- **THEN** the budget derivation sees no upper bound for that stage — the raw projected state is narrowed by the same authority predicates before the read — and the decision remains the retry decision instead of the blocked demotion

#### Scenario: Tied contributors keep the upper bound alive through filtering

- **WHEN** a stage's maximum attempt is carried by two tied contributing rows outside the window — a fresher row that identity filtering judges non-authoritative and an older candidate-authoritative row
- **THEN** the filtered state still carries the stage's upper bound at that maximum, because at least one contributor survives the predicates

#### Scenario: The failure policy binds to the carried upper bound

- **WHEN** the reverse truncation geometry carries a stage upper bound `N` at the retry limit and the candidate's own in-window row fails with a transient error code on that stage
- **THEN** the failure policy reports attempt `N` with automatic retry disallowed and permanent/limit-exhausted set — the intended decision-level consequence of truthful attempt derivation — where the pre-change projection reported attempt zero and a retriable failure

#### Scenario: Manual retry mints from the carried upper bound when the stage is nameable

- **WHEN** the failed stage is nameable from the filtered state (a candidate-authoritative failed or cancelled row sits inside the window) while the stage's maximum-attempt row `N` sits outside the window, and an adopted manual-retry marker carries no explicit attempt
- **THEN** the mint derives `previous_attempt == N` and `new_attempt == N+1` instead of the pre-change window-local value

#### Scenario: Explicit stage reads ignore an unrelated flat retry count

- **WHEN** a candidate projection's top-level flat `retry_count` is 5 solely because of a `convert` row and the state has no forecast or download retry attempt
- **THEN** explicit `forecast` and `download` attempt reads both return 0 under large and truncated windows
- **AND** a stage-less read keeps the existing flat-first result without making that value a stage budget

#### Scenario: Stage-less flat-first derivation is unaffected

- **WHEN** the stage-less attempt derivation runs against a projected state carrying non-empty stage upper bounds
- **THEN** it returns the flat-first value exactly as before — the carried upper bounds never leak into stage-less reads
