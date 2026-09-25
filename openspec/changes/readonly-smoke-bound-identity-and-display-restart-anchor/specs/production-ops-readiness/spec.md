## MODIFIED Requirements

### Requirement: Display API restart is reproducible from a single command

The node-27 display API uvicorn restart path SHALL be a single, repo-committed shell script (`scripts/ops/start-display-api.sh`) that:

1. Sources `infra/env/display.env` via `set -a; . ...; set +a` so every key is exported to the relaunched process environment.
2. Asserts required env keys (`DATABASE_URL`, `NHMS_ENABLE_LIVE_POSTGIS_MVT`) are present before launch and aborts non-zero with an explicit missing-keys list if not (does not leak values).
3. Gracefully replaces the prior `apps.api.main:app` uvicorn process of **this checkout** (SIGTERM with bounded wait, SIGKILL fallback) before relaunching detached via `setsid` so the new process survives SSH disconnect. A process belongs to this checkout only when its command line starts with the resolved repository root's `.venv/bin/python -m uvicorn apps.api.main:app`; the script SHALL NOT signal any other process, including another checkout's display uvicorn running as the same user.
4. Runs a post-launch smoke check (`curl /api/v1/models?limit=1` → `jq .data.items[0].basin_id != null`) that surfaces env-drift contract regressions (the exact failure mode PR #596 fixed) before user-facing breakage.

The runbook `docs/runbooks/display-readonly-live-mvt.md` SHALL reference this script for the "restart service" step and SHALL NOT reference dangling host-only paths (e.g. `/tmp/start_display.sh`) that are not committed to the repo.

The systemd-unit alternative (`/etc/systemd/system/nhms-display-api.service`) MAY be added in a follow-up change when operator-account sudo on node-27 is available; that alternative is out of scope for this requirement.

#### Scenario: Restart from single command sources env file and asserts contract

- **WHEN** the operator runs `bash scripts/ops/start-display-api.sh` on node-27 from any working directory
- **THEN** the script resolves the repo root and sources `infra/env/display.env` with `set -a` export
- **AND** the script aborts non-zero before launch if `DATABASE_URL` or `NHMS_ENABLE_LIVE_POSTGIS_MVT` is missing
- **AND** the script gracefully terminates any prior `apps.api.main:app` uvicorn of this checkout before relaunching
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

#### Scenario: Restart leaves another checkout's uvicorn running

- **WHEN** the operator runs `bash scripts/ops/start-display-api.sh` while another checkout's `<other>/.venv/bin/python -m uvicorn apps.api.main:app` process runs as the same user
- **THEN** that process is never signalled, whichever relaunch path the script takes
- **AND** a stale `apps.api.main:app` uvicorn started from this checkout's own `.venv/bin/python` is still stopped before the relaunch
- **AND** a repository root containing regular-expression metacharacters matches only itself
