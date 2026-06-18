## ADDED Requirements

### Requirement: Return-period index audit report

The system SHALL provide an operator-facing way to generate a `flood.return_period_result` index audit report without executing destructive database maintenance.

#### Scenario: Audit report captures index inventory

- **WHEN** the operator runs the audit workflow against a PostgreSQL database containing `flood.return_period_result`
- **THEN** the report includes root relation size, index size, known index names, index definitions, `pg_stat_user_indexes` usage counters, and pre/post size SQL snippets.

#### Scenario: Timescale chunk metadata is unavailable

- **WHEN** Timescale chunk metadata queries fail or TimescaleDB views are unavailable
- **THEN** the report still includes root-table catalog evidence and marks chunk-level evidence as unavailable with the captured reason.

### Requirement: Hot-path query-plan evidence

The audit workflow SHALL include query-plan probes or generated `EXPLAIN (ANALYZE, BUFFERS)` SQL for the documented return-period hot paths.

#### Scenario: Core query probes are generated

- **WHEN** the operator asks for the maintenance evidence bundle
- **THEN** the output includes probes for summary, ranking/segments, timeline, GeoJSON fallback tile, MVT selected identity, valid-time discovery, and latest-ready-run quality behavior.

#### Scenario: Query safety is preserved

- **WHEN** hot-path probes require runtime identifiers such as `run_id`, `duration`, `valid_time`, or river segment identity
- **THEN** the generated probe template uses bind parameters or clearly marked placeholders rather than interpolating untrusted values into SQL.

### Requirement: Index classification and manual maintenance plan

The system SHALL classify known `flood.return_period_result` indexes and generate a manual maintenance plan that is safe for operator review.

#### Scenario: Known indexes are classified

- **WHEN** the audit workflow sees indexes from migrations 000015, 000020, 000021, 000031, or 000034
- **THEN** each known index is classified as keep, drop, rebuild, replace, or investigate with a reason and mapped hot-path usage where applicable.

#### Scenario: NULL partial indexes are flagged

- **WHEN** a `return_period_result` index primarily covers NULL `return_period` or NULL `warning_level` rows
- **THEN** the plan flags it as a high-priority drop/investigate candidate unless query-plan evidence proves it is required.

#### Scenario: Maintenance SQL is operator-gated

- **WHEN** the workflow generates SQL for dropping, rebuilding, reindexing, vacuuming, repacking, chunk rebuilding, or compression
- **THEN** the SQL is written as a manual maintenance artifact with `lock_timeout` guidance, failure recovery notes, and explicit text that it must not be auto-executed by application startup, migrations, or CI.

#### Scenario: Connection mode does not bypass approval

- **WHEN** the workflow is configured with readonly or writer database access
- **THEN** readonly mode is limited to audit/report evidence and writer mode still produces only manual artifacts unless the operator performs the maintenance SQL outside the audit workflow.

### Requirement: Production runbook evidence

The production runbook SHALL document post-cleanup index maintenance and space recovery expectations for `flood.return_period_result`.

#### Scenario: DELETE does not imply disk release

- **WHEN** an operator reads the runbook after #490 cleanup
- **THEN** it states that row deletion alone may leave dead tuples and index bloat, and that disk recovery requires a separate approved maintenance step.

#### Scenario: Before/after evidence is required

- **WHEN** an operator prepares a maintenance window
- **THEN** the runbook lists required before/after evidence for database size, table size, chunk/index size, query-plan checks, and production regression checks.
