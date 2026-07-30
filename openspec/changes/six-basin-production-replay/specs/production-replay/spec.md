# production-replay — Spec Delta

## ADDED Requirements

### Requirement: Replay admission is env-gated, closed-set, and inert by default

The scheduler SHALL activate replay admission only when `NHMS_SCHEDULER_REPLAY_MODE` is truthy, `NHMS_SCHEDULER_REPLAY_MODEL_IDS` parses to a non-empty closed set, `NHMS_SCHEDULER_REPLAY_WINDOW` parses to a valid cycle range, and the pass pins exactly one cycle via `--cycle-time`. When all replay variables are absent the scheduler's candidate decision chain SHALL be byte-identical to pre-change behavior. When `NHMS_SCHEDULER_REPLAY_MODE` is truthy but any companion variable is missing or malformed, the pass SHALL fail closed with a typed configuration error and submit nothing.

#### Scenario: Default environment is untouched behavior

- **WHEN** a scheduler pass runs with none of the replay variables set
- **THEN** candidate decisions, discovery, completion skipping, and evidence output are identical to the pre-change scheduler for the same inputs

#### Scenario: Partial replay configuration fails closed

- **WHEN** `NHMS_SCHEDULER_REPLAY_MODE=1` but `NHMS_SCHEDULER_REPLAY_MODEL_IDS` is unset or empty
- **THEN** the pass exits with a typed configuration error before submitting any work

### Requirement: Replay terminal override is candidate-scoped and journal-preserving

When replay admission is active, the scheduler SHALL replace the strict-warm-start terminal-success decision branch — including its mismatch, successor-retry, and retry-budget-exhausted outcomes — with a `replay_resubmit` decision, only for candidates whose `model_id` is in the replay set, whose cycle lies inside the replay window, and whose state decision is in the strict-warm-start terminal-skip family (`terminal_hydro_success`, the shape production journals actually yield for succeeded runs, and `terminal_pipeline_success`); the `terminal_completed_cycle` decision is explicitly outside the override family and results in no resubmission, which the replay driver surfaces as a halt rather than a silent skip. The override SHALL carry a forecast-stage restart, SHALL NOT consult or consume the persisted retry budget, SHALL record a typed `replay_terminal_override` evidence entry that includes the overridden branch's original shape, and its evidence SHALL survive the downstream run-manifest retry-upgrade check unaltered (native resubmission marked true, durable output not reused) so the decision token reaches the chain unchanged. Candidates outside the set or window, and candidates whose state decision is not in the override family, SHALL follow the unchanged decision chain. The replay path SHALL NOT delete, rewrite, or relocate any journal file; prior journal records remain in place as the replacement audit trail.

#### Scenario: In-scope candidate is re-admitted without journal mutation

- **WHEN** a replay pass targets a cycle whose journal yields the `terminal_hydro_success` decision shape for a candidate whose model is in the replay set
- **THEN** the candidate is decided `replay_resubmit` with a forecast-stage restart, the evidence records `replay_terminal_override`, and every pre-existing journal file for that cycle is byte-identical after admission

#### Scenario: Out-of-family terminal decision is a surfaced halt, not a silent skip

- **WHEN** an in-scope candidate's state decision resolves to `terminal_completed_cycle`
- **THEN** no override is emitted, the candidate is not resubmitted, and the replay driver halts on its convergence timeout with the interruption recorded in the receipt

#### Scenario: Successor-retry shape cannot reuse stale forecast output

- **WHEN** an in-scope candidate's terminal decision would, without replay mode, resolve to a state-save-stage resume because the successor checkpoint is not ready
- **THEN** the replay override still restarts at the forecast stage and no `state_save_qc`-only resume is emitted for that candidate

#### Scenario: Exhausted historical retry budget does not block replay

- **WHEN** an in-scope candidate's persisted journal records carry a retry count at or above the configured limit
- **THEN** the replay decision is still `replay_resubmit` and no `strict_warm_start_retry_budget_exhausted` block is emitted

#### Scenario: Out-of-scope model in the same cycle keeps the unchanged decision

- **WHEN** the same replay pass evaluates a candidate whose model is not in the replay set
- **THEN** that candidate's decision is identical to what the pre-change scheduler would produce for the same inputs

### Requirement: Replay cycle admission without a raw manifest requires full forcing evidence

When replay admission is active and the pinned cycle has no NFS raw manifest, the scheduler SHALL admit the cycle only if the direct-grid forcing package for every model in the replay set is present and non-empty under a bounded no-follow check. If any forcing package is missing, empty, or the check cannot be completed, the whole cycle SHALL be rejected with a typed reason and nothing SHALL be submitted; an uncompletable probe SHALL be reported as undeterminable, never folded into a proven-missing result. The scheduler SHALL NOT fall back to ordinary discovery or attempt raw conversion for that cycle. When the raw manifest is present, the pre-existing admission gate applies unchanged. Admission SHALL survive downstream candidate assembly: when canonical readiness evaluates to a fresh zero-row result for the pinned cycle, a candidate carrying the replay override SHALL NOT be blocked by the raw-manifest-required gate regardless of the raw-manifest-required flag — the replay forcing evidence stands in for the absent raw manifest, and the guard SHALL verify that substitute is actually present and ready; a raw-less admission whose substituting forcing evidence is absent or not ready SHALL be blocked with a typed reason rather than admitted. For a replay-window cycle whose candidate carries an authorized missing-forcing repair decision with a verified-present raw manifest, the fresh-zero-row canonical state SHALL NOT block the candidate; it SHALL instead be admitted with a convert-stage restart that rebuilds canonical and forcing from the raw manifest. Outside the replay window both behaviors are unchanged.

#### Scenario: Historical cycle admitted on forcing evidence

- **WHEN** a replay pass pins a cycle whose raw manifest is absent but all replay-set forcing packages exist and are non-empty
- **THEN** the cycle is admitted and replay candidates proceed

#### Scenario: One missing forcing package rejects the whole cycle

- **WHEN** the pinned cycle has no raw manifest and at least one replay-set model's forcing package is missing or unreadable
- **THEN** the pass records a typed rejection for the cycle and submits nothing

#### Scenario: Canonical-incomplete raw-less cycle still yields submitted replay candidates

- **WHEN** a replay pass pins a raw-less cycle whose canonical readiness evaluates to a fresh zero-row result and all replay-set forcing packages are present
- **THEN** the replay candidates survive candidate assembly with the `replay_resubmit` decision and a forecast-stage restart, and no raw-manifest-required block is emitted for them

#### Scenario: Raw-less admission without a present substitute is blocked, not admitted

- **WHEN** a candidate reaches the raw-less assembly leg but the discovery record carries no ready replay forcing evidence
- **THEN** the candidate is blocked with a typed reason and the guard evidence does not claim a substitute that is absent

#### Scenario: Replay-covered repair cycle is admitted via convert-stage restart

- **WHEN** a replay-window cycle carries an authorized missing-forcing repair decision with a verified-present raw manifest and canonical readiness evaluates to a fresh zero-row result
- **THEN** the candidate is admitted with a convert-stage restart that rebuilds canonical and forcing from raw, and no missing-forcing-package block is emitted

### Requirement: Replay candidates restart from the forecast stage

The `replay_resubmit` decision SHALL be a member of the chain's force-terminal-resubmit decision set so that a terminal-success pipeline is re-submitted rather than resumed as a no-op, with pre-existing terminal-success `convert` and `forcing` journal records honored as stage reuse while `forecast` and state-save stages re-execute as new jobs. The first-cycle initial-state decision contract SHALL be consumed unchanged: with the state scope cleared, the first replayed cycle takes the packaged-IC bootstrap path exactly as specified by the forecast-warm-start capability, and replay admission SHALL NOT alter that decision.

#### Scenario: Resume no-op is prevented

- **WHEN** a replay candidate is admitted for a cycle whose journal holds succeeded forecast-stage records from the original run
- **THEN** the chain's force-resubmit evaluation accepts the `replay_resubmit` decision and the forecast stage is re-submitted as a new job rather than resumed as already complete

#### Scenario: First replayed cycle consumes the packaged IC

- **WHEN** the state index holds no entry for the replay candidate's model and source and the packaged IC qualifies
- **THEN** the decision is `PACKAGED_IC_BOOTSTRAP` and the resulting run manifest records `init_mode=3` with `quality=packaged_calibrated_state`

#### Scenario: Raw-manifest-ready cycle keeps the forecast-stage restart

- **WHEN** a replay candidate's cycle has a ready raw manifest but canonical readiness evaluates to a fresh zero-row result, so raw-manifest restart evidence would otherwise apply a convert-stage restart
- **THEN** the `replay_resubmit` decision retains its forecast-stage restart and the succeeded convert and forcing journal records remain reused rather than resubmitted

### Requirement: Scoped state-chain reset is archived, dual-lane, and fail-closed

The state-scope reset tool SHALL default to dry-run and mutate only under an explicit enforce flag. Before any mutation it SHALL verify the scheduler timer is not active (an undeterminable probe counts as active), snapshot both state-index lanes byte-for-byte, record every to-be-removed entry, and record a three-way stat/sha256 result plus a byte archive for each affected state object. Removal SHALL cover exactly the requested (model, source) scopes in both lanes, leave all other entries byte-identical, use atomic write-back with post-write read-back verification, and emit a schema-versioned receipt. A failed read-back SHALL be reported as commit-uncertain, never as refused.

#### Scenario: Dry-run performs zero writes

- **WHEN** the reset tool runs without the enforce flag
- **THEN** both index files and all state objects are byte-identical afterwards and the receipt reports `enforced=false`

#### Scenario: Unreadable state object does not block the reset

- **WHEN** one in-scope state object cannot be stat'ed during archiving
- **THEN** the receipt records the three-way probe result for that object, the index entry is still removed, and the tool completes

#### Scenario: Read-back failure is commit-uncertain

- **WHEN** the post-write read-back of an index lane fails
- **THEN** the tool exits with the commit-uncertain code and the receipt says commit-uncertain, not refused

### Requirement: Replacement traceability receipt

The replay driver SHALL maintain a schema-versioned replacement receipt with one row per (model, source, cycle) capturing, before overwrite: the prior run-manifest sha256, prior output inventory digests, the prior state entry (state id, checksum, created-at) sourced from the reset receipt's removed-entries record (never read back from the already-cleared index, and never silently defaulted), the prior terminal journal job id, and — unconditionally — the forcing package checksum and model package checksum consumed by that cycle; and after replay: the new manifest sha256, new state checksum, init mode, quality, and the key-consistency assertion result over evidence the run actually carries (river-network version, output segment count, output file inventory), with `packaged_ic_checksum` populated on first-cycle rows. Cycles with no prior run SHALL be recorded as such rather than omitted. The receipt SHALL reference the reset receipts, record the driver's start-time inventory census, and record any interruption point. The driver SHALL verify the receipt path is writable before its first submission and refuse otherwise; a later receipt-write failure SHALL halt the sequence with a non-zero exit, never continue silently. On resumption, any row already captured in the resume receipt SHALL retain its recorded prior half regardless of row status — the driver SHALL NOT re-capture a prior from a run tree that has already been overwritten — and the driver SHALL refuse to write its receipt to the same path it resumes from. Loading a reset receipt SHALL verify its scope list covers every (model, source) of the current run and refuse on mismatch.

#### Scenario: First-cycle row proves the bootstrap

- **WHEN** the replay of a scope's first cycle completes
- **THEN** its receipt row records `init_mode=3`, `quality=packaged_calibrated_state`, and a non-empty `packaged_ic_checksum`, and a mismatch halts the driver

#### Scenario: Absent prior run is recorded, not skipped

- **WHEN** a replayed cycle had no pre-existing run tree
- **THEN** the receipt row carries the no-prior-run marker with the new-side fields populated

### Requirement: Serial replay execution is retention-safe and convergence-gated

The replay driver SHALL refuse to start unless the effective environment disables retention. It SHALL process cycles strictly serially per source: a cycle is complete only when the journal shows terminal success for every replay-set model and the NFS state index holds fresh entries for the successor valid time; any failure or timeout halts the sequence with the receipt recording the interruption, and resumption SHALL verify previously completed cycles by evidence rather than skipping blindly.

#### Scenario: Retention-enabled environment is refused

- **WHEN** the driver starts and `NHMS_RETENTION_ENABLED` is not `false`
- **THEN** it exits with an error before invoking any scheduler pass

#### Scenario: Next cycle waits for index convergence

- **WHEN** a replayed cycle's jobs succeed but the NFS index does not yet hold the six successor state entries
- **THEN** the driver keeps waiting (up to its timeout) and does not submit the next cycle

### Requirement: node-27 refresh serves replayed results

Before re-ingest, compressed TimescaleDB chunks intersecting the replay window SHALL be surveyed and decompressed via the existing decompression-replay surface, with a receipt; otherwise the parser's replacement delete fails closed against compressed chunks. After replay, the node-27 pipeline SHALL re-ingest every replayed run, and map-tile cache entries scoped to the replayed run sources **and the national aggregate layer** SHALL be invalidated over a valid-time window that extends through the last replayed cycle plus the full forecast display horizon, so that the display API and frontend serve only replay-derived data for the affected scopes. Verification SHALL assert: database rows reference the new manifests with first-cycle rows in bootstrap shape; each replayed (run, variable) series carries a single river-network-version/variable key set with no stale-key remnants; and database rows for out-of-scope basins are unchanged.

#### Scenario: Compressed chunks are cleared before re-ingest

- **WHEN** the pre-ingest survey finds compressed chunks intersecting the replay window
- **THEN** those chunks are decompressed and receipted before any replacement ingest runs, and no ingest batch fails on the compressed-chunk guard

#### Scenario: Stale tiles are not served after invalidation

- **WHEN** re-ingest completes and the tile invalidation tool runs scoped to the replayed run sources plus the national aggregate layer over the full display-horizon window
- **THEN** subsequent tile requests in that scope are rebuilt from replayed data rather than served from pre-replay cache rows

#### Scenario: Out-of-scope basins unchanged

- **WHEN** replay verification runs its negative checks
- **THEN** sampled runs, state entries, and stored display series for the twelve out-of-scope models are byte-identical to their pre-replay state
