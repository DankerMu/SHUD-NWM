# production-ops-readiness Specification

## Purpose
TBD - created by archiving change m10-production-closure. Update Purpose after archive.
## Requirements
### Requirement: Production configuration is validated before release

The system SHALL provide production environment templates and validation checks for all deployable services.

#### Scenario: Production config validates required services

- **WHEN** production readiness validation runs
- **THEN** API, orchestrator, Slurm gateway, tile publisher, frontend, database, object store, source adapters, and workspace roots are checked for required settings
- **AND** missing or unsafe settings fail with stable error codes and no secret disclosure

### Requirement: Operator actions are backend gated and audited

The system SHALL enforce or explicitly gate backend-side authorization for production-impacting actions.

#### Scenario: Production action requires authorized role

- **WHEN** a user attempts model activation, rerun, cancel, QC override, source config change, or tile republish
- **THEN** backend authorization verifies the required role or the action is blocked by a documented release gate
- **AND** successful actions write audit evidence with actor, role, target, previous/new state, and redacted lineage

#### Scenario: Unauthorized production action is denied

- **WHEN** a user without the required role attempts a production-impacting action
- **THEN** the backend returns a stable unauthorized or forbidden response
- **AND** the action does not mutate model state, pipeline jobs, QC override state, source config, or tile publication state
- **AND** the audit or security log records the denied attempt without secret values

#### Scenario: Deferred auth is a release blocker

- **WHEN** full backend auth cannot be completed in this change
- **THEN** the issue must emit a release-blocker artifact listing deferred actions, current fallback, required roles, residual risk, and the condition required to remove the gate

### Requirement: Monitoring and alerts cover closure risks

The system SHALL expose metrics and alert rules for production data, compute, object store, API, and publication failures.

#### Scenario: Production closure alerts are testable

- **WHEN** validation injects or observes source latency, Slurm queue backlog, basin failure, object-store write failure, stale analysis state, tile publication error, or API p95 breach
- **THEN** the corresponding metric and alert rule identify severity, target, current value, threshold, and recommended operator action

### Requirement: Runbooks and rollback drills are present

The system SHALL document and verify rollback procedures for common production closure failures.

#### Scenario: Rollback drill records outcome

- **WHEN** a rollback drill is run for bad model activation, failed publish/import, failed source cycle, failed Slurm array, or bad tile release
- **THEN** the runbook records commands, preconditions, expected evidence, recovery result, and residual risk

### Requirement: Display API restart is reproducible from a single command

The node-27 display API uvicorn restart path SHALL be a single, repo-committed shell script (`scripts/ops/start-display-api.sh`) that:

1. Sources `infra/env/display.env` via `set -a; . ...; set +a` so every key is exported to the relaunched process environment.
2. Asserts required env keys (`DATABASE_URL`, `NHMS_ENABLE_LIVE_POSTGIS_MVT`) are present before launch and aborts non-zero with an explicit missing-keys list if not (does not leak values).
3. Gracefully replaces the prior `apps.api.main:app` uvicorn process (SIGTERM with bounded wait, SIGKILL fallback) before relaunching detached via `setsid` so the new process survives SSH disconnect.
4. Runs a post-launch smoke check (`curl /api/v1/models?limit=1` → `jq .data.items[0].basin_id != null`) that surfaces env-drift contract regressions (the exact failure mode PR #596 fixed) before user-facing breakage.

The runbook `docs/runbooks/display-readonly-live-mvt.md` SHALL reference this script for the "restart service" step and SHALL NOT reference dangling host-only paths (e.g. `/tmp/start_display.sh`) that are not committed to the repo.

The systemd-unit alternative (`/etc/systemd/system/nhms-display-api.service`) MAY be added in a follow-up change when operator-account sudo on node-27 is available; that alternative is out of scope for this requirement.

#### Scenario: Restart from single command sources env file and asserts contract

- **WHEN** the operator runs `bash scripts/ops/start-display-api.sh` on node-27 from any working directory
- **THEN** the script resolves the repo root and sources `infra/env/display.env` with `set -a` export
- **AND** the script aborts non-zero before launch if `DATABASE_URL` or `NHMS_ENABLE_LIVE_POSTGIS_MVT` is missing
- **AND** the script gracefully terminates any prior `apps.api.main:app` uvicorn before relaunching detached via `setsid`
- **AND** the script asserts `curl /api/v1/models?limit=1 | jq .data.items[0].basin_id` is non-null and exits non-zero on null/missing/parse error
- **AND** the relaunched uvicorn process environment contains `DATABASE_URL`

#### Scenario: Restart smoke check catches env drift before user-facing breakage

- **WHEN** the operator launches via `bash scripts/ops/start-display-api.sh` and the env-sourced `DATABASE_URL` resolves to a database where `core.basin_version` JOIN cannot populate `basin_id` for any active model
- **THEN** the smoke check `jq .data.items[0].basin_id != null` returns false and the script exits non-zero
- **AND** the operator is alerted in the same restart command output, not after frontend popup breakage in production

#### Scenario: Restart smoke check tolerates empty model registry

- **WHEN** the operator launches via `bash scripts/ops/start-display-api.sh` and `/api/v1/models?limit=1` returns `data.items` with length 0 (no active models registered yet — typical on a fresh DB)
- **THEN** the script logs a warning ("/api/v1/models returned 0 items; basin_id smoke check skipped (DB may be empty)") and exits 0
- **AND** the operator-visible message instructs separate model-registration verification before declaring restart healthy

### Requirement: Diagnostic restart paths defer to the canonical wrapper and probe /health

Any repo-committed shell or Python script that restarts the node-27 hand-launched display API uvicorn for diagnostic, measurement, or smoke purposes SHALL defer to `scripts/ops/start-display-api.sh` for the relaunch rather than inlining its own `setsid python -m uvicorn ...` launcher. Diagnostic scripts MAY add additional pre/post measurement logic around the wrapper call, but the act of stopping the prior uvicorn, sourcing `infra/env/display.env`, and relaunching MUST go through the canonical wrapper.

Any repo-committed script that probes the display API health endpoint as part of a wait-loop, sanity check, or measurement table SHALL probe `/health` (root path, registered by `apps/api/main.py:1947` `_register_static_and_health_routes`). Scripts MUST NOT probe `/healthz`, `/api/v1/health`, or other non-existent variants that return 404 on a healthy uvicorn (which would silently degrade health-wait logic and pollute measurement evidence with 404 dispatch overhead).

#### Scenario: Diagnostic script consolidates relaunch on wrapper

- **WHEN** `scripts/diagnostic/display-cold-waterfall.sh` (or any equivalent diagnostic script) needs to relaunch the display API uvicorn between measurement passes
- **THEN** its relaunch function MUST call `bash "${NWM_ROOT}/scripts/ops/start-display-api.sh"` instead of inlining `setsid .venv/bin/python -m uvicorn apps.api.main:app ...`
- **AND** the wrapper's preflight + env-sourcing + graceful SIGTERM + smoke check are inherited automatically (no parallel hand-launch shape)

#### Scenario: Diagnostic health probe uses /health root

- **WHEN** a diagnostic or measurement script waits for the display API to become healthy after a restart
- **THEN** the probe URL is `${BASE}/health` (root)
- **AND** the probe is NOT `${BASE}/healthz` or `${BASE}/api/v1/health` (both 404 on healthy uvicorn — would silently fail the wait-loop or measure dispatch overhead instead of health-check TTFB)
- **AND** any ENDPOINTS array, docstring, output table, or sequence text in the same script that references the health endpoint uses `/health` consistently

### Requirement: node-27's applied migration ledger MUST cover every shipped migration before code that reads a new column is served

node-27 SHALL NOT serve display or ingest code that projects a column whose migration is absent from
`public.schema_migrations`; the pending migration set SHALL be applied first, in its own timer-stopped window.

Display and ingest code on node-27 is deployed by moving a checkout forward, not by a release artifact, so a
migration that ships in `db/migrations/` but is never applied leaves the database behind the code with no
mechanism that surfaces it. `services/tiles/mvt.py::national_discharge_source_version` and
`::national_river_network_source_version` project `core.river_network_version.geometry_generation`, and
`workers/model_registry/basins_registry_import.py::_backfill_output_segment_geometry` updates it inside the
import transaction — the read side fails on the next display restart, the write side on the next ingest tick,
neither waiting for the other.

The pending set SHALL be computed as `db/migrations/*.sql` minus `public.schema_migrations`, and applied with
`packages.common.migrate` connected as the `nhms` owner role, so the ledger is written; `psql -f` SHALL NOT be
used to apply, because it leaves the ledger unchanged and lets the next bring-up silently replay. Rows present
in the ledger with no file on disk are `#2048`'s retroactive-deletion drift, are never visited by
`packages/common/migrate.py`'s loop, and SHALL NOT block applying the pending set. The apply SHALL be wrapped
by a role audit on both sides — audit-only before the superuser write session, and the full
`scripts/node27_provision_write_roles.sh` with a clean strict audit after — and SHALL run inside a window with
the ingest and download timers stopped. Acceptance SHALL be read from the database's own catalogs (applied
ledger rows, `information_schema.columns`, `timescaledb_information.dimensions`), never from the apply
command's exit code alone.

#### Scenario: a national read route is served against a database missing a projected column

- **WHEN** display code that projects `core.river_network_version.geometry_generation` serves
  `hydro-national/{variable}`, `hydro-national/{source}/{cycle}`, `river-network-national` or `/api/v1/layers`
  against a node-27 database whose ledger does not contain the migration that adds that column
- **THEN** the three national tile routes return 500 unconditionally — each computes its digest before fetching
  the tile — and `/api/v1/layers` returns 500 whenever a display-ready run exists, returning 200 with an empty
  layer list otherwise, because `apps/api/routes/hydro_display.py` returns early on `display_ready_run(session) is None`
  and never reaches the digest; and the recorded remedy is to apply the pending migration set with
  `packages.common.migrate` in a timer-stopped window before serving that code, not to restart the service or
  to roll the checkout back

#### Scenario: the pending set is applied and verified from the catalogs

- **WHEN** the pending set is applied on node-27 with `packages.common.migrate` as the `nhms` owner role
- **THEN** every pending filename appears in `public.schema_migrations`, `core.river_network_version.geometry_generation`
  reads `integer` / `is_nullable = NO` / default `0` with no row distinct from 0, the full
  `scripts/node27_provision_write_roles.sh` exits 0 with a clean strict audit, and the same four surfaces
  answer with zero 500 — the three national tile routes returning 200, or the expected 424 when no
  display-ready run exists, and `/api/v1/layers` returning 200 in either case

### Requirement: Re-pointing node-27 runtime units at a different checkout MUST be verified from the units' effective configuration and MUST roll back as one set

When node-27's user units are moved between checkouts by adding or removing systemd drop-ins, the operator SHALL
treat every unit that carries such a drop-in as one set: the set SHALL be enumerated from
`~/.config/systemd/user/*.d/*.conf`, not from the two units people remember, and the move SHALL be verified from
`systemctl --user show` of every unit's effective `ExecStartPre`, `ExecStart`, `ExecStartPost`, `ExecStop`,
`WorkingDirectory`, `Environment`, `EnvironmentFiles` and `DropInPaths`, never from "the script exists in the target tree".

The expected post-move configuration SHALL be derived mechanically from the pre-move capture — substitute the old
checkout path with the new one and drop only the tokens the drop-in itself injected — and compared verbatim with the
post-move capture. Any other residual (a lost flag, a missing environment file, a unit still carrying a drop-in) is a
gate failure. On a gate failure, or on any failure of the display API's read surfaces after restart, every removed
drop-in and every edited env file SHALL be restored from the backup taken before removal and the display API
restarted under the restored configuration; the set SHALL NOT be left half-moved. Foreign operational fences
(capacity holds, other operators' drop-ins, state directories) SHALL NOT be created, removed or modified by the move;
their observed state SHALL be recorded.

The display API restart in such a move SHALL NOT sweep processes by a pattern that can match another checkout's
`apps.api.main:app` instance on the same host; when the canonical restart wrapper does so, the operator SHALL
reproduce its systemd branch by hand and record the deviation.

#### Scenario: eight units carry the same-named drop-in and only the display unit is unpinned

- **WHEN** an operator removes `nhms-display-api.service.d/60-reslice-pin-original-5a86841c.conf` and restarts the
  display API, but leaves the same-named drop-in under the seven timer-driven units
- **THEN** the display API serves the active checkout while ingest, download, retention, compression, governance and
  the frontier alert keep running the frozen copy, and the mechanical pre/post diff of all eight units' effective
  configuration is non-empty — the move is incomplete and MUST NOT be reported as an unpin

#### Scenario: the drop-in set is removed and the effective configuration matches the derived expectation

- **WHEN** all drop-ins of the set are backed up and removed, `daemon-reload` runs, and the post-move capture equals
  the pre-move capture with the checkout path substituted and the drop-in-injected tokens removed
- **THEN** the display API is restarted by the systemd branch of the canonical procedure, its main process's `cwd`,
  `exe` and `NHMS_MVT_FILE_CACHE_DIR` are read from `/proc/<MainPID>/`, the other instance on the host keeps its PID,
  and the read surfaces (`/api/v1/layers`, `river-network-national`, both `hydro-national` routes) answer with zero
  500 before any timer is re-enabled; a failure at any of these points restores the whole set from the backup

