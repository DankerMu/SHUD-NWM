# runtime-service-role-boundary Specification

## Purpose

Which role an API process runs in (`dev_monolith`, `compute_control`, `display_readonly`, `slurm_gateway`), what each role may
expose and with which configuration it refuses to start, the non-superuser database roles of the node-27 write paths, and how
the display deployment on node-27 is shown to run in the display role (the C1 receipt).

## Requirements

### Requirement: Service role configuration

The system SHALL expose a single runtime service role contract that distinguishes local monolith, compute control, display readonly, and Slurm gateway execution modes.

#### Scenario: Local development default
- **WHEN** the API starts without `NHMS_SERVICE_ROLE` in a non-production local/test environment
- **THEN** it uses `dev_monolith`
- **AND** existing local tests can still exercise mock Slurm and mutating workflows.

#### Scenario: Production role required
- **WHEN** the API starts with `NHMS_REQUIRE_SERVICE_ROLE=true` or with production auth mode such as `NHMS_AUTH_MODE=production`, `live`, or `live_idp` and without `NHMS_SERVICE_ROLE`
- **THEN** startup fails with a clear configuration error
- **AND** the service does not silently fall back to a role that exposes control-plane capabilities.

#### Scenario: Docker and systemd set explicit roles
- **WHEN** Docker compose or systemd examples start an API service
- **THEN** they set `NHMS_REQUIRE_SERVICE_ROLE=true`
- **AND** they set an explicit `NHMS_SERVICE_ROLE` matching the service being started.

#### Scenario: Unknown role rejected
- **WHEN** `NHMS_SERVICE_ROLE` is set to an unsupported value
- **THEN** startup fails with a clear configuration error
- **AND** no API routes are served.

### Requirement: Slurm route exposure by role

The API SHALL mount Slurm control routes only for roles that are allowed to expose control-plane behavior.

#### Scenario: Display readonly has no Slurm routes
- **WHEN** the API starts with `NHMS_SERVICE_ROLE=display_readonly`
- **THEN** `/api/v1/slurm/*` routes are not registered
- **AND** the display-mode OpenAPI schema does not advertise Slurm operations.

#### Scenario: Compute control can expose Slurm routes
- **WHEN** the API starts with `NHMS_SERVICE_ROLE=compute_control`
- **THEN** Slurm routes can be registered according to existing gateway configuration
- **AND** existing control-plane tests can call the Slurm health route.

#### Scenario: Dev monolith remains compatible
- **WHEN** the API starts with `NHMS_SERVICE_ROLE=dev_monolith`
- **THEN** existing local Slurm mock and integration tests keep their current route surface unless a test explicitly overrides the role.

### Requirement: Display role unsafe configuration guard

The display readonly role SHALL reject or clearly block configuration that would give 27 compute-control capability.

#### Scenario: Display role has Slurm gateway configured
- **WHEN** `NHMS_SERVICE_ROLE=display_readonly` and `SLURM_GATEWAY_URL` or `SLURM_GATEWAY_BACKEND=slurm` is configured
- **THEN** startup fails or the configuration is reported as a blocker before serving requests
- **AND** retry/cancel and `/api/v1/slurm/*` remain unavailable.

#### Scenario: Display role has forbidden compute paths
- **WHEN** `NHMS_SERVICE_ROLE=display_readonly` and compute-only paths such as `WORKSPACE_ROOT`, `NHMS_BASINS_ROOT`, or `SHUD_EXECUTABLE` are configured as active dependencies
- **THEN** startup or preflight reports a high-severity display boundary blocker
- **AND** the service does not rely on those paths for display readiness.

### Requirement: Runtime config API

The API SHALL expose a read-only runtime config contract for frontend capability gating.

#### Scenario: Display runtime config
- **WHEN** the frontend requests runtime configuration from a display readonly API
- **THEN** the response identifies `service_role=display_readonly`
- **AND** it reports `control_mutations_enabled=false`, `slurm_routes_enabled=false`, and a display-safe queue-depth mode.

#### Scenario: Compute runtime config
- **WHEN** the frontend requests runtime configuration from a compute-control or dev API
- **THEN** the response identifies the current service role
- **AND** it reports whether control mutations and Slurm routes are enabled for that role.

### Requirement: Slurm gateway role is bounded

The `slurm_gateway` role SHALL not accidentally start the full business API surface.

#### Scenario: Reserved gateway role
- **WHEN** an implementation has not added a dedicated Slurm Gateway ASGI app
- **THEN** `NHMS_SERVICE_ROLE=slurm_gateway` is treated as a reserved role for host-service documentation
- **AND** startup fails clearly rather than serving forecast, pipeline, model, or frontend routes.

#### Scenario: Dedicated gateway app
- **WHEN** a dedicated Slurm Gateway app is implemented in this change
- **THEN** its route inventory contains only health and `/api/v1/slurm/*` gateway routes
- **AND** it does not expose business read or write APIs.

### Requirement: node-27 write-path components SHALL authenticate as non-superuser roles provisioned idempotently

Every recurring node-27 runtime unit that writes to the production database SHALL connect as a role with `rolsuper`, `rolcreaterole`, `rolcreatedb`, `rolreplication` and `rolbypassrls` all false, holding the measured privilege set for its component (ownership of the application-schema relations plus DML grants and default privileges for the ingest-class lanes, DML grants and default privileges on `met` for the download lane); the roles, grants, default privileges and ownership SHALL be provisioned by one idempotent script whose trailing audit fails on drift; the superuser role SHALL appear in no runtime env file — in DSN or `PGUSER` form — except the documented migration-class exceptions (archive-rebuild drill, compression replay supervisor). Retired cold-residency runtime and its tablespace `CREATE` grant/positive grant audit SHALL NOT be provisioned or required; all unrelated security audits SHALL remain intact. This source retirement SHALL NOT authorize live privilege revocation or tablespace/data deletion.

#### Scenario: Provision is idempotent

- **WHEN** the provision script is run twice against the same database
- **THEN** the second run completes without error and the audit output is identical

#### Scenario: Migration-added tables stay usable before re-provision

- **WHEN** a migration run as `nhms` creates a new table in an application schema and the provision script has not yet been re-run
- **THEN** `nhms_ingest_rw` can read and write that table through default privileges, only its stats-guard ANALYZE entry reports `warning`, and the audit reports the owner drift

#### Scenario: Display grants survive the ownership transfer

- **WHEN** the ownership loop has run
- **THEN** `nhms_display_ro`'s `SELECT` privilege set over the application schemas is identical to the captured pre-transfer set, each relation's transfer committed on its own so no display query waited longer than one relation's `lock_timeout`, and any relation left untransferred after the retry passes is reported by the audit and blocks the cutover

#### Scenario: Role membership regression is refused

- **WHEN** either write role is granted membership in any other role (for example `GRANT nhms TO nhms_ingest_rw` or `GRANT pg_read_server_files TO nhms_ingest_rw`) and the provision script runs in any mode
- **THEN** the flags audit raises a security-regression error naming the role and the membership, and the runner exits 3 — the audit reads `pg_auth_members`, not only the `pg_roles` flags

#### Scenario: Owner-planted rules and triggers are refused and audited

- **WHEN** either write role attempts `CREATE RULE` or `CREATE TRIGGER` on a relation it owns
- **THEN** on ordinary tables and on TimescaleDB chunks the superuser-owned event trigger raises and no rule or trigger is created, the write role cannot drop that event trigger, and a migration run as `nhms` can still create triggers; on hypertables (and the internal `_compressed_hypertable_*` relations) TimescaleDB's utility hook processes `CREATE TRIGGER` itself and the event trigger does not fire, so a planted trigger is created and propagates to the chunks — there the guarantee is detection only: the strict audit fails on any rule (other than view `_RETURN` rules) or any non-internal trigger (other than TimescaleDB's `ts_insert_blocker`) in the six application schemas, or on any `_timescaledb_internal` relation owned by either write role, that is not on the explicit allowlist of the four `met` triggers from migration 000043, on any allow-listed trigger that is not enabled, and on any column default, `CHECK` constraint (including one added `NOT VALID`, which skips the owner-side validation but is still evaluated for new rows), rule action or trigger function in those relations that references a function untrusted for a superuser writer — one in a temp schema, one whose owner is not a superuser, one the write role cannot itself `EXECUTE`, or one whose `(schema, name)` is not on the explicit allow-list of functions the migrations reference (the four `met.*` trigger functions and `pg_catalog.{btrim,float8,gen_random_uuid,int8,nextval,now}` (ten, all derived from `db/migrations/**`; the ledger-backed `jsonb_typeof` was removed when `000064` dropped the `flood` CHECKs that referenced it)), the only exemption being a non-volatile `pg_catalog` operator implementation reached solely through `:opfuncid`; a PUBLIC-executable superuser-owned `pg_catalog` function such as `query_to_xml` planted in a column default is therefore reported as `not on the migration allow-list` — (the `ALTER TABLE … SET DEFAULT` / `ADD CONSTRAINT … NOT VALID` forms of the same gadget, which the event trigger does not cover because the cold-residency lane needs `ALTER TABLE`); TimescaleDB's `ts_insert_blocker` is recognised by function identity, not by name, so a replaced blocker is reported as a planted trigger, and the four allow-listed triggers must be present as well as enabled; after the full-mode provision the write roles hold no `TEMP` privilege on the database (`REVOKE TEMPORARY … FROM PUBLIC` + re-grant to `nhms_display_ro`), so they can author no function body at all, while `--roles-only` leaves `TEMP` untouched

#### Scenario: Write roles granted to other roles are refused

- **WHEN** either write role is granted to any other role (for example `GRANT nhms_ingest_rw TO nhms_display_ro`)
- **THEN** the flags audit raises a security-regression error naming both roles and the runner exits 3

#### Scenario: Retired cold tablespace grant is not provisioned or required

- **WHEN** ordinary provisioning runs whether or not an old `nhms_cold` tablespace exists
- **THEN** it neither grants `CREATE` for the retired lane nor demands that grant in a positive audit, and unrelated strict role/security audits remain enforced
- **AND** it does not revoke live privileges or `DROP` an existing tablespace as part of this source retirement

#### Scenario: Program execution is closed

- **WHEN** either write role attempts `COPY … FROM PROGRAM`
- **THEN** the server refuses with a superuser-required error

#### Scenario: Runtime env files carry no superuser

- **WHEN** the ingest, download, compression, and retention env files on node-27 are inspected
- **THEN** none names the `nhms` user in any credential form, the templates in `infra/env/` name the same roles as the live files, and the drill and compression-replay files are the only ones still naming `nhms`, each with its documented reason

#### Scenario: Each component runs under its role

- **WHEN** ingest, compression, and retention each execute one real run under `nhms_ingest_rw`, and download under `nhms_download_rw`
- **THEN** each completes with its normal receipt, no `permission denied`, and the ingest tick's stats guard reports both ANALYZE legs as `ok`

### Requirement: The display deployment receipt observes the running unit

The node-27 display deployment receipt (C1 of the bring-up checklist) SHALL be
the output of `scripts/ops/display-c1-receipt.sh`. The script SHALL only read:
it SHALL NOT start, stop or restart a unit or a process, SHALL NOT source
`infra/env/display.env`, and SHALL NOT print any value from that file other
than the two storage roots named below. It SHALL NOT call docker.

The repository root SHALL be the checkout that contains the script, unless a
root is given as the first argument. The receipt SHALL print the root and the
checkout's HEAD commit.

The script SHALL evaluate every claim below, print one line per claim with
`ok` or `FAIL`, and then print `C1=PASS` and exit zero only if every claim is
`ok`; otherwise it SHALL print `C1=FAIL` and exit non-zero.

| Claim | Holds when |
|---|---|
| `unit_active` | the user unit `nhms-display-api.service` is active and has a main process |
| `process_of_checkout` | the main process's command line is `<root>/.venv/bin/python -m uvicorn apps.api.main:app` with a `--port` |
| `checkout_clean` | no tracked file of the checkout is modified, staged or deleted |
| `process_loaded_head` | the main process started later than the checkout's HEAD last moved and later than `infra/env/display.env` was last modified, each by more than one second as measured in whole seconds |
| `listener_of_unit` | at least one socket listens on that port, every listening line names its processes, and each of them is the main process or a child of it |
| `health` | `GET /health` on the loopback address and that port answers 200 |
| `runtime_role` | `GET /api/v1/runtime/config` reports `service_role` `display_readonly`, `display_readonly` true, `slurm_routes_enabled` false and `control_mutations_enabled` false |
| `slurm_routes_absent` | `GET /api/v1/slurm/health` answers 404 |
| `no_slurm_client` | none of `sbatch`, `scancel`, `squeue`, `scontrol`, `sacct`, `srun`, `sinfo` is on `PATH`, and none of `/etc/slurm`, `/etc/slurm-llnl`, `/run/munge/munge.socket.2` exists |
| `storage_not_writable` | `OBJECT_STORE_ROOT` and `NHMS_PUBLISHED_ARTIFACT_ROOT` of `infra/env/display.env` each name an existing directory that the invoking user cannot write |

`storage_not_writable` is a statement about the two root directories
themselves, not about every file beneath them.

The start time is the observation time minus the elapsed time of the main
process, both in whole seconds, and can therefore come out one second later
than the real start. The one-second margin of `process_loaded_head` covers
that rounding: a process measured exactly one second after the HEAD move or
the env file change does not pass.

A claim whose input cannot be read SHALL be `FAIL`, never skipped: no main
process, no port, an empty reflog, an unreadable env file, a storage key that
is missing or empty, a listener query that fails or names nobody, a request
that gets no answer.

#### Scenario: A healthy display unit passes

- **WHEN** the unit is active and its main process is the clean checkout's uvicorn, started after the checkout's HEAD last moved and after the env file was last modified
- **AND** the port is held by that process and its children only, the three requests answer as the table says, the host has no Slurm client, and neither storage root is writable
- **THEN** every claim line says `ok`, the last line is `C1=PASS`, and the exit status is zero

#### Scenario: A process older than the checkout does not pass

- **WHEN** the checkout's HEAD moved after the main process started, in the same second, or one measured second before it
- **THEN** `process_loaded_head` is `FAIL`, the last line is `C1=FAIL`, and the exit status is non-zero
- **AND** the other claims are still evaluated and printed

#### Scenario: A process older than its env file does not pass

- **WHEN** `infra/env/display.env` was modified after the main process started, in the same second, or one measured second before it
- **THEN** `process_loaded_head` is `FAIL` and the receipt does not pass

#### Scenario: A modified checkout does not pass

- **WHEN** a tracked file of the checkout differs from HEAD
- **THEN** `checkout_clean` is `FAIL` and the receipt does not pass
- **AND** an untracked file alone does not fail the claim

#### Scenario: A foreign listener does not pass

- **WHEN** a process that is neither the main process nor one of its children listens on the port
- **THEN** `listener_of_unit` is `FAIL` and the receipt does not pass, whatever the three requests answer

#### Scenario: A reachable Slurm route does not pass

- **WHEN** `GET /api/v1/slurm/health` answers anything other than 404
- **THEN** `slurm_routes_absent` is `FAIL` and the receipt does not pass

#### Scenario: A role other than display_readonly does not pass

- **WHEN** `GET /api/v1/runtime/config` reports another `service_role`, or `display_readonly` false, or `slurm_routes_enabled` true, or `control_mutations_enabled` true
- **THEN** `runtime_role` is `FAIL` and the receipt does not pass

#### Scenario: A Slurm client on the host does not pass

- **WHEN** one of the listed commands is on `PATH`
- **THEN** `no_slurm_client` is `FAIL` and the receipt does not pass

#### Scenario: A writable storage root does not pass

- **WHEN** the invoking user can write the object-store root or the published-artifact root, or one of the two keys is missing
- **THEN** `storage_not_writable` is `FAIL` and the receipt does not pass

#### Scenario: An inactive unit fails every claim that needs the process

- **WHEN** the unit is not active or has no main process
- **THEN** `unit_active`, `process_of_checkout`, `process_loaded_head`, `listener_of_unit`, `health`, `runtime_role` and `slurm_routes_absent` are `FAIL`
- **AND** no request is sent to a guessed port

#### Scenario: The receipt changes nothing

- **WHEN** the script runs, passing or failing
- **THEN** it has called no `systemctl` verb other than `show` and `is-active`, has sent only `GET` requests, and has written no file, the checkout's git index included
