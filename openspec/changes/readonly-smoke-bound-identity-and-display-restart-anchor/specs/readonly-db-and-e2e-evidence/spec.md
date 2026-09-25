## MODIFIED Requirements

### Requirement: Display readonly DB validation

The display service SHALL be validated with readonly database credentials for display APIs.

#### Scenario: Readonly display smoke
- **WHEN** the API starts with `NHMS_SERVICE_ROLE=display_readonly` and a readonly DB user
- **THEN** read-only display routes such as health, runtime config, models, stations, latest-product, pipeline status, pipeline stages, jobs, and job logs can be exercised
- **AND** latest-product, pipeline status, pipeline stages, jobs, and job logs PASS evidence is scoped to one strict `source`, `cycle_time`, `run_id`, and `model_id` identity, with job logs also scoped to `job_id`
- **AND** missing strict identity fields make the identity-bound route evidence `BLOCKED`, not `PASS`
- **AND** an identity-bound route is `PASS` only when its response is 2xx and its response identity echo carries every strict field and agrees with the requested identity by field meaning: `source` case-insensitively, `cycle_time` as the same UTC cycle hour whatever its spelling (`Z` or `+00:00`), and every other field exactly
- **AND** no display smoke step requires writing hydro, met, or pipeline terminal state.

#### Scenario: Mutating API blocked with readonly DB
- **WHEN** retry or cancel is called on the display API backed by readonly DB credentials
- **THEN** the request returns `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`
- **AND** it does not fail later because of a database write attempt.

#### Scenario: Readonly DB evidence
- **WHEN** readonly DB validation runs
- **THEN** evidence records the DB role type, `current_user`, redacted database URL, commands, and pass/fail/blocker status
- **AND** secrets are redacted.

#### Scenario: Write privileges denied
- **WHEN** readonly DB validation runs against hydro, met, ops, and pipeline-critical tables
- **THEN** catalog inventory is gathered for all probed tables, columns, all sequences in audited schemas, required schemas, current database `CREATE`, and reachable roles before any DML or DDL probe is executed
- **AND** table-level `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, and supported non-read privileges such as `MAINTAIN` are treated as mutating capability
- **AND** column-level `INSERT` and `UPDATE`, any `USAGE` or `UPDATE` grant on sequences in `hydro`, `met`, or `ops`, schema `CREATE` on every probed schema, and current database `CREATE` are treated as mutating capability
- **AND** reachable roles that can be inherited or set by the tested login are treated as mutating capability when they have unsafe role attributes or mutating privileges on the audited surfaces
- **AND** controlled `INSERT`, `UPDATE`, `DELETE`, and DDL probes are rejected by DB permissions or readonly transaction semantics before commit when catalog inventory is clean
- **AND** rollback is cleanup only, not proof of readonly behavior
- **AND** any successful DML or DDL execution under the tested credential is recorded as `FAIL` even if the harness rolls it back
- **AND** any catalog mutating capability under the tested credential or reachable role is recorded as `FAIL` and prevents DML or DDL probes anywhere in the matrix
- **AND** any audited-schema sequence `USAGE` or `UPDATE` privilege under the tested credential or reachable role is recorded as `FAIL` without executing `nextval`, `setval`, DML, or DDL
- **AND** a display PASS cannot be claimed by merely labeling a writer credential as readonly.

#### Scenario: Identity echo judged per field

- **WHEN** an identity-bound route answers 2xx
- **THEN** the route evidence records the response identity it extracted from one response object, even when some strict fields are absent, and never combines fields from different response objects
- **AND** each absent strict field is reported by name as a missing-identity blocker and makes the route `BLOCKED`, with no blocker for fields that are present and agree
- **AND** each strict field that contradicts the requested identity is reported with its expected and observed values and makes the route `FAIL`
- **AND** a `cycle_time` of `2026-09-24T12:00:00Z` agrees with a requested `2026-09-24T12:00:00+00:00`

#### Scenario: Error responses carry no identity-echo verdict

- **WHEN** an identity-bound route answers with a non-2xx status
- **THEN** its verdict comes from the error code alone: `BLOCKED` for a declared fixture or published-artifact blocker code, otherwise `FAIL`
- **AND** the route evidence carries no identity-echo blockers

#### Scenario: Discovered identity is self-consistent

- **WHEN** readonly DB validation discovers the display identity from the database instead of taking every field from the operator
- **THEN** the discovered run is the newest display-ready run, restricted to the configured source or business run id when one is configured
- **AND** the discovered `job_id`, when present, belongs to that run and has a recorded log URI, and no job of another run is ever paired with it
- **AND** the latest-product smoke requests the discovered run's basin
- **AND** fields the operator configures still override the discovered ones

#### Scenario: Deny-write and read-route verdicts are reported separately

- **WHEN** readonly DB validation or the per-source merge writes its summary after probes ran
- **THEN** the summary reports a deny-write lane status (role, permission probes, manual-action probes) and a read-route lane status (route smoke) next to the overall status
- **AND** for a single live run the overall status is the worst of the two, so a read-route `BLOCKED` never hides a deny-write `FAIL` and a clean deny-write lane is visible when a read route is not clean
- **AND** a simulated summary is never `PASS`, whatever its lane statuses
- **AND** the per-source merge refuses a source bundle that is not `PASS`, and a merged summary's status is `BLOCKED` on any merge blocker while its lane statuses carry the worst verdict of the merged items
