## ADDED Requirements

### Requirement: The automatic retry identity differs between the non-master and the current-master path on purpose

When the journal schedules an automatic retry for a row whose durable record is not a current-contract
forecast master (forcing and the downstream stages, forecast candidate rows, and forecast rows without a
current accepted-submit contract, a legacy master row included), it SHALL take the failed row's job id as
given, without stripping a retry suffix, and append `_retry_<n>` with `n` one more than the `retry_count`
of the caller's snapshot of that row. The durable row's count and the suffix already carried by the id
SHALL NOT enter `n`, and `n` SHALL NOT be clamped.

When the journal schedules an automatic retry for a current-contract forecast master, the identity it
derives SHALL strip the last retry suffix and append `_retry_<n>` with `n` one more than the effective
attempt of the row (the larger of its last suffix and its recorded count). The cycle's inline retry of a
master strips the last suffix as well; it counts with the inline formula, whose `failure_number - 1` term
is pinned as it is and is not decided here.

The non-master and the current-master derivation give different ids for the same input. This is intended
(#3319, decided 2026-11-26): stacked ids are a produced shape of long standing, every parser takes the
LAST `_retry_<n>` suffix of an id as the attempt it carries, and journals in the field hold stacked rows.
Unifying the paths is a behaviour change that SHALL go through its own issue and SHALL show, for every
consumer of the id, that no occupied id is minted again and no stage's attempt count restarts.

#### Scenario: The non-master path stacks

- **WHEN** a non-master row `job_1_retry_1` fails and the caller's snapshot carries `retry_count` 1
- **THEN** the automatic retry is `job_1_retry_1_retry_2` with count 2

#### Scenario: The non-master count comes from the caller's snapshot alone

- **WHEN** the durable row of `job_1_retry_1` records `retry_count` 5 and the caller's snapshot carries 0
- **THEN** the automatic retry is `job_1_retry_1_retry_1` with count 1

#### Scenario: The non-master count has no lower bound

- **WHEN** the caller's snapshot of `job_1_retry_1` carries `retry_count` -1
- **THEN** the automatic retry is `job_1_retry_1_retry_0` with count 0

#### Scenario: The current-master derivation strips

- **WHEN** the identity of the next automatic retry is derived for the current master `X_retry_1` with
  recorded count 1
- **THEN** it is `X_retry_2` with count 2

#### Scenario: The two derivations differ on the same input

- **WHEN** both derivations are given the job id `X_retry_1` and the count 1
- **THEN** the non-master one gives `X_retry_1_retry_2` and the current-master one gives `X_retry_2`

#### Scenario: A stacked id parses as its last suffix

- **WHEN** a parser splits `X_retry_1_retry_2`
- **THEN** the attempt is 2 and the base is `X_retry_1`

### Requirement: An upstream refresh re-runs a terminal stage only from failed, submission_failed or partially_failed

A terminal stage row MAY be run again because its upstream stage finished again only when the row's
status is `failed`, `submission_failed` or `partially_failed`, and only when the upstream finished at or
after the row's terminal time or the row carries no terminal time. A `permanently_failed` or
`cancelled` row SHALL NOT be run again for that reason: `cancelled` records an operator's intent and
`permanently_failed` a spent retry budget or a non-transient error, and a refreshed upstream overrides
neither. Both statuses remain sources of a manual retry.

This set differs from the manual-retry source set by exactly `permanently_failed` and `cancelled`. The
difference is intended (#3319, decided 2026-11-26); widening the set is a behaviour change that SHALL go
through its own issue.

#### Scenario: A failed stage is re-run after its upstream refreshed

- **WHEN** a stage row is `failed`, `submission_failed` or `partially_failed`, and its upstream stage
  finished again at or after the row's terminal time
- **THEN** the row may be run again

#### Scenario: A row of the set without a terminal time is re-run

- **WHEN** a stage row is `failed`, `submission_failed` or `partially_failed`, carries no terminal
  time, and its upstream stage finished again
- **THEN** the row may be run again

#### Scenario: A permanently failed or cancelled stage is not

- **WHEN** a stage row is `permanently_failed` or `cancelled`, and its upstream stage finished again,
  whether after the row's terminal time or with no terminal time on the row
- **THEN** the upstream refresh does not run the row again
- **AND** a manual retry may still select it

#### Scenario: No refresh, no re-run

- **WHEN** the upstream stage has not finished again, or finished before the row's terminal time
- **THEN** the upstream refresh does not run the row again, whatever its status

## MODIFIED Requirements

### Requirement: Retry Identity — Same run_id, New slurm_job_id

Retry jobs SHALL maintain continuity with the original run while obtaining fresh Slurm resources.

#### Scenario: Retry preserves run_id

- **WHEN** a retry job is created (either manual or automatic)
- **THEN** the new `pipeline_job` SHALL use the same `run_id` as the original failed job
- **THEN** the new `pipeline_job` SHALL use the same `cycle_id`, `source`, `stage`, and `model_id` as the original

#### Scenario: Retry gets new slurm_job_id

- **WHEN** a retry job is submitted to Slurm
- **THEN** a new `slurm_job_id` SHALL be assigned by Slurm and stored in the new `pipeline_job` record
- **THEN** the original failed job's `slurm_job_id` SHALL remain unchanged for audit purposes

#### Scenario: Retry gets new job_id

- **WHEN** a retry job is created
- **THEN** a new `job_id` SHALL be generated for the retry job as the deterministic string `<base>_retry_<n>`, where
  `n` is the retry attempt and is at least 1
- **THEN** the one exception to "at least 1" SHALL be the journal's non-master automatic retry, whose `n` is one more than
  the caller snapshot's `retry_count` and is not clamped (requirement "The automatic retry identity differs between the non-master and the current-master path on purpose")
- **THEN** when a `job_id` carries more than one `_retry_<n>` suffix, the last suffix SHALL be the authoritative attempt
- **THEN** the relationship between original and retry jobs SHALL be traceable via `pipeline_event.details_json.previous_job_id`

### Requirement: Retry and attempt accounting SHALL have one owner

`services/orchestrator/retry_identity.py` SHALL be the single owner of retry
and attempt accounting. It owns:

- retry job id formatting;
- next-attempt minting on the stage-scoped, floored, inline, current-master,
  non-master and manual paths;
- stage attempt reads over already-read rows;
- the cohort restart stage;
- the default retry-limit constant;
- the manual-retryable and upstream-refresh-retryable status sets.

Other modules SHALL delegate to the owner. The operator overrides
`NHMS_SCHEDULER_RETRY_LIMIT` and `SLURM_GATEWAY_MAX_RETRIES` SHALL keep their
current readers and precedence. The owner's functions SHALL take plain data.
The only IO seam SHALL be an injected occupancy predicate for the floored
mint. Moving a computation into the owner SHALL NOT change its result for any
input.

#### Scenario: Historical retry cases are pinned in one table

- **WHEN** the owner's table-driven test runs
- **THEN** plain-data rows cover:
  - the prefix-switch and cohort-digest budget cases (#2404);
  - the sibling-model, inline-stacking and occupied-id cases (#2404, #1201);
  - mixed-floor cohorts (#2542);
  - stacked and malformed suffixes (#2254);
  - explicit context precedence (#2393);
  - suffix-over-count when the recorded count is absent;
  - the cohort restart stage (#2394, #2416);
  - the default limit with each env override.
- **AND** an index row names the existing integration test for each of these
  cases and asserts that the test exists:
  - the sibling and own model-set forcing resume (#1845);
  - the nested partial retry, spent budget and concurrent-pass cases (#1845);
  - the manual-claim gate without an active decision (#1201);
  - the selected-snapshot retry identity (#1356).
- **AND** each plain-data row's expected value equals what the pre-move code
  returns for the same input.

#### Scenario: Divergent paths are pinned as they are

- **WHEN** the table includes any of these paths:
  - the non-master automatic retry, which derives the next attempt from the
    caller snapshot's `retry_count` alone;
  - the inline-retry formula's `failure_number - 1` term;
  - the two `rsplit("_retry_", 1)` increments in the forecast cycle;
  - the difference between the chain's hard-coded retryable status sets and
    `retry.py`'s sets.
- **THEN** those rows assert today's results, divergences included
- **AND** changing any of them requires a separate behaviour-change issue that
  edits the row and this requirement.
- **AND** the first of these paths, and the content of the upstream-refresh status set
  named in the last one, are kept on purpose (#3319, decided 2026-11-26) and are stated
  by the requirements "The automatic retry identity differs between the non-master and the current-master path on purpose" and
  "An upstream refresh re-runs a terminal stage only from failed, submission_failed or partially_failed"; an issue that changes either
  edits that requirement too.
- **AND** the `failure_number - 1` term, the two `rsplit` increments, and the fact that
  `retry.py` keeps status sets of its own are pinned as they are without such a decision.

#### Scenario: A call site computes an attempt locally

- **WHEN** a module outside `retry_identity.py` builds a job id from the
  `_retry_` literal or `RETRY_JOB_ID_MARKER` in an f-string, concatenation,
  `%` or `.format`, or `join`, or splits on that marker, or adds an integer literal to
  the result of any `retry_identity` function, of any call whose name contains
  `attempt` (the scheduler-state readers such as `_state_retry_attempt` and
  `_fallback_previous_attempt`), or of a split on that marker
- **THEN** an AST guard test fails and names the module and line
- **AND** calls to the owner's parsers, regex matches and `in` membership tests on
  the marker that do not mint a new id or attempt are allowed.
- **AND** the guard is a syntactic check: every form T2 removed is caught, and
  any shape it is later found to miss is recorded in the guard and closed by
  tightening it, never by an allowlist entry.
