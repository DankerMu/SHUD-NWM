# readonly-db-and-e2e-evidence Specification

## Purpose
How the node-27 display is proven read-only and how that evidence is laid out: the readonly-DB validator's deny-write probes, its identity-bound display route smoke and self-consistent identity discovery, the per-source merge, and where Docker and E2E evidence bundles live.

## Requirements

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
- **THEN** the discovered run is the newest display-ready run, restricted to the configured source when one is configured, or, when a business run id is configured, exactly that run whatever its status
- **AND** the discovered `job_id`, when present, belongs to that run and has a recorded log URI, and no job of another run is ever paired with it
- **AND** the latest-product smoke requests the discovered run's basin
- **AND** fields the operator configures still override the discovered ones

#### Scenario: Deny-write and read-route verdicts are reported separately

- **WHEN** readonly DB validation or the per-source merge writes its summary after probes ran
- **THEN** the summary reports a deny-write lane status (role, permission probes, manual-action probes) and a read-route lane status (route smoke) next to the overall status
- **AND** for a single live run the overall status is the worst of the two, so a read-route `BLOCKED` never hides a deny-write `FAIL` and a clean deny-write lane is visible when a read route is not clean
- **AND** a simulated summary is never `PASS`, whatever its lane statuses
- **AND** the per-source merge refuses a source bundle that is not `PASS`, and a merged summary's status is `BLOCKED` on any merge blocker while its lane statuses carry the worst verdict of the merged items

### Requirement: Docker and E2E evidence location

All project-created Docker smoke, codeagent review, and two-node E2E artifacts SHALL be written under the repository or `/scratch/frd_muziyao`.

#### Scenario: Stage change review artifacts
- **WHEN** OpenSpec review or planning agents produce output
- **THEN** the output is saved under `artifacts/stage-change/m22-two-node-docker-readonly-display/` or `/scratch/frd_muziyao/...`
- **AND** no project-created temporary evidence is written to the system disk by default.

#### Scenario: Docker smoke artifacts
- **WHEN** Docker build, compose config, or container security smoke tests run
- **THEN** test logs and generated evidence are written under the repository `artifacts/` tree or `/scratch/frd_muziyao`
- **AND** commands document any unavoidable Docker daemon cache usage separately.

#### Scenario: Docker disk preflight
- **WHEN** Docker build or compose smoke is about to run
- **THEN** evidence records `docker version`, `docker compose version`, DockerRootDir from `docker info`, `docker system df`, and relevant `df -h`
- **AND** low available space marks Docker smoke as `BLOCKED` before large build steps run.

#### Scenario: Two-node E2E artifacts
- **WHEN** a two-node E2E run is executed
- **THEN** evidence uses `artifacts/two-node-e2e/<run_id>/` or an explicitly configured `/scratch/frd_muziyao/...` root
- **AND** evidence separates compute control, display service, cross-plane, manual ops boundary, Docker, DB, API, browser, Slurm, and logs.

### Requirement: Cross-plane pass gates

The two-node Docker E2E SHALL only pass when display readonly boundaries and strict run identity are proven.

#### Scenario: Display security gate
- **WHEN** the 27 Docker display service is validated
- **THEN** the evidence proves no Slurm route, no retry/cancel execution, no Slurm/Munge/Docker socket capability, readonly DB use, and readonly published artifact access
- **AND** failure of any boundary marks display service E2E as fail.

#### Scenario: Cross-plane run identity gate
- **WHEN** cross-plane E2E is evaluated
- **THEN** 27 latest-product, the current `/` display entrypoint, `/hydro-met -> /` only as a legacy redirect compatibility check when included, `/ops`, and job logs must point to the same `run_id/source/cycle_time/model_id` produced by 22
- **AND** historical latest or mocked API data cannot satisfy the pass condition.

#### Scenario: GFS and IFS pass scope
- **WHEN** the two-node E2E plan includes both GFS and IFS for a run
- **THEN** both sources must pass strict identity latest-product, series, ops, logs, and browser source-switch checks before cross-plane status is `PASS`
- **AND** a single-source run is reported as reduced scope or `PARTIAL`, not full cross-plane `PASS`.

#### Scenario: Manual ops boundary gate
- **WHEN** retry/cancel behavior is validated
- **THEN** 27 proves fail-closed read-only behavior and 22 proves any actual retry/cancel receipt
- **AND** 27 only displays the resulting state and logs after 22 acts.

### Requirement: Reachable-roles probe MUST execute on a real PostgreSQL

The statement the readonly-DB validation lane uses to discover the roles reachable by the tested login SHALL be
executable by a real PostgreSQL server, and the lane's regression suite SHALL prove that against a real database
rather than against a recording double. Satisfying string assertions on the statement text is not evidence that the
probe runs.

#### Scenario: Reachable-roles probe runs against a live database

- **WHEN** the readonly-DB validation lane gathers reachable roles for the tested login on a real PostgreSQL
- **THEN** the statement SHALL parse and execute, returning the reachable-role rows
- **AND** it SHALL NOT use a PostgreSQL reserved word as a relation alias
- **AND** an automated test SHALL execute that probe against a real PostgreSQL, so restoring a reserved-word alias
  fails the suite instead of surfacing only as a `BLOCKED` verdict with an unexpected-error blocker on production

### Requirement: The readonly validation DSN MUST carry its libpq options in a form libpq accepts

The readonly-DB validation lane SHALL encode the bounded connection options it writes into the display
connection URL so that a libpq client recovers the intended option string. Encoding conventions that only a
form decoder understands MUST NOT be used for this value.

The lane's regression suite SHALL prove this by opening a real connection with the emitted URL through the
libpq-direct client, not only through a driver that applies form decoding of its own.

#### Scenario: A bounded connection URL is handed to a libpq client

- **WHEN** the lane rebuilds the display connection URL with its bounded connection options and a client
  passes that URL directly to libpq
- **THEN** percent-decoding the emitted options value alone SHALL yield the lane's intended option string
  verbatim
- **AND** the connection SHALL open, reporting the lane's configured statement, lock and
  idle-in-transaction timeouts
- **AND** an automated test SHALL execute that connection against a real PostgreSQL, so reintroducing a
  form-encoded options value fails the suite instead of surfacing only as a connection `FATAL` on production
