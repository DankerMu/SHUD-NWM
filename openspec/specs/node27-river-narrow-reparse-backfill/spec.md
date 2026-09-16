# node27-river-narrow-reparse-backfill Specification

## Purpose
TBD - created by archiving change node27-river-narrow-reparse-backfill. Update Purpose after archive.
## Requirements
### Requirement: Reparse SHALL flip a run's route and write its narrow facts in one transaction

The backfill runner SHALL process each candidate run in a single database transaction: lock the `hydro.hydro_run`
row, re-check eligibility under the lock, set `timeseries_store = 'narrow'`, run the production output parser on the
same connection, and verify that the narrow row count for the run's `run_key` equals the parser's rows written (> 0),
that the run's status is unchanged, and that its route is `narrow`, before committing. Any error or failed check SHALL
roll the transaction back, leaving route, facts, status and `parsed_at` unchanged, and SHALL be recorded as `failed`
with an error code.

#### Scenario: Successful reparse
- **WHEN** a `published` run routed `legacy` with its `.rivqdown` artifact present is processed
- **THEN** the run is routed `narrow`, its narrow facts equal the legacy facts, its status stays `published`, and its
  legacy facts are untouched

#### Scenario: Missing artifact
- **WHEN** the run's artifact is missing
- **THEN** the outcome is `failed` and the run's route, facts, status and `parsed_at` are exactly as before

#### Scenario: Post-parse verification fails
- **WHEN** the verification after the parse detects any inconsistency
- **THEN** the transaction is rolled back and the outcome is `failed` with `VERIFY_MISMATCH`

### Requirement: Backfill scope SHALL be the retention window and aged-out runs SHALL stay legacy

Candidates SHALL be runs routed `legacy` with `parsed_at` set, status `published` or `superseded`, and `end_time`
later than now minus `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS`, ordered newest cycle first, optionally narrowed by
`--end-time-after`. The same predicate SHALL be re-evaluated under the row lock. A run outside it SHALL NOT be flipped.

#### Scenario: Aged-out run
- **WHEN** a legacy-routed run's `end_time` is older than the retention window
- **THEN** it is not a candidate, a direct attempt reports `aged_out`, and it stays `legacy`

#### Scenario: Non-terminal status
- **WHEN** a legacy-routed run is `parsed` rather than `published` or `superseded`
- **THEN** it is not reparsed

### Requirement: The run command SHALL be operator-gated and hold the lifecycle mutex

`run` SHALL refuse without the GO token, before connecting. It SHALL acquire the timeseries lifecycle mutex before any
mutation, refuse with no mutation when the mutex is held, and hold it until the run ends. Under the mutex, it SHALL
decompress every compressed narrow chunk that overlaps the candidates' time range when their pre-compression total is
within `--max-decompress-bytes`, and otherwise refuse with no mutation. It SHALL never decompress or write the legacy
table.

#### Scenario: Mutex held by a lifecycle tick
- **WHEN** compression or retention holds the mutex
- **THEN** `run` refuses with `LIFECYCLE_LOCK_CONTENDED` and no run changes

#### Scenario: Compressed overlap
- **WHEN** a compressed narrow chunk overlaps the backfill range within budget
- **THEN** it is decompressed, recorded in the summary, and the overlapping runs are reparsed; over budget, the
  command refuses and the chunk stays compressed

### Requirement: The runner SHALL be resumable and bounded, with secret-free receipts

Each outcome SHALL be appended and fsynced to `runs.jsonl`. The run SHALL stop dispatching at `--deadline`, on
SIGTERM/SIGINT, or after `--max-failures` failures, and SHALL write a summary with dispositions, rows written,
undispatched count, stop reason, legacy route counts before and after, and tool and parser hashes. A later invocation
SHALL resume from the database state without repeating committed runs. Receipts SHALL NOT contain DSNs.

#### Scenario: Deadline then resume
- **WHEN** a run stops at its deadline with runs undispatched and is started again
- **THEN** the first summary is `partial` with stop reason `deadline`, and the second processes only runs still
  routed `legacy`

#### Scenario: Failure budget
- **WHEN** failures reach `--max-failures`
- **THEN** dispatch stops with stop reason `failure_budget` and exit code 1

### Requirement: Verify SHALL compare legacy and narrow values read-only

`verify` SHALL, in a read-only transaction, report for each given or sampled reparsed run its route, narrow row
count, legacy row count, and the number of legacy rows without an identical narrow row (value, unit, quality flag,
lead time). A run SHALL pass only when routed `narrow` with narrow rows and zero mismatches.

#### Scenario: Divergent value
- **WHEN** a narrow value differs from its legacy value
- **THEN** that run's verdict is `fail` with the mismatch count, and the command exits 1

