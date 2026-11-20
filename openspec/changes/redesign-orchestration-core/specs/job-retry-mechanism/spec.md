## ADDED Requirements

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
The only IO seam SHALL be an injected occupied-id predicate for the floored
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

#### Scenario: A call site computes an attempt locally

- **WHEN** a module outside `retry_identity.py` builds a job id from the
  `_retry_` literal or `RETRY_JOB_ID_MARKER` in an f-string or concatenation,
  or adds to the result of `split_retry_job_identity`, `retry_suffix_attempt`,
  `effective_retry_attempt`, or `rsplit("_retry_", ...)`
- **THEN** an AST guard test fails and names the module and line
- **AND** parsing calls that do not mint a new id or attempt are allowed.
