## ADDED Requirements

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
