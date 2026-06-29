## Context

Issue #625 follows the 2026-06-21 stale-warning banner in
`docs/runbooks/current-production-ops.md`. The banner correctly warns operators
not to follow the old node-22-writer body, but the body still contains stale
commands and path assumptions.

Live verification on 2026-06-22 found:

- node-27 repo `/home/nwm/NWM` at `1be0f05`, tracked worktree clean.
- node-27 has active PostgreSQL on `:55432` and display DB URL
  `postgresql://nhms_display_ro:<redacted>@127.0.0.1:55432/nhms` for the
  display API.
- node-27 runs `scripts/node27_autopipe_cron.sh` every 10 minutes from crontab.
  That wrapper runs `scripts/node27_autopipeline.py`, scanning
  `/home/ghdc/nwm/object-store/runs`, seeding basin registry data, registering
  and parsing runs, publishing display status, and running coverage refresh.
- node-27 display API is the hand-launched `apps.api.main` uvicorn managed by
  `scripts/ops/start-display-api.sh`; live correction changed
  `infra/env/display.env` from `NHMS_DISPLAY_API_PORT=8000` to `8080` to match
  repo template/wrapper defaults and nginx `test.nwm.ac.cn` proxy config. The
  wrapper smoke check and public `/health` both passed after restart.
- node-27 also has `nwmops.service`, but that service is `/home/nwm/NWM_Ops`
  and is not the NHMS repo orchestrator/ingest path for this runbook.
- node-22 repo `/scratch/frd_muziyao/NWM` at `1be0f05`, tracked worktree clean.
- node-22 runs `services.slurm_gateway` and a diagnostic `apps.api.main` on
  `:8001`. Its historical PostgreSQL `:55433` has since been archived and
  stopped by #837; it remains out of current NHMS production topology and must
  not be used by current DB query examples.
- node-22 sees NFS roots at `/ghdc/data/nwm/object-store` and
  `/ghdc/data/nwm/published`; node-27 sees the same data under
  `/home/ghdc/nwm/object-store` and `/home/ghdc/nwm/published`.

## Goals / Non-Goals

**Goals:**

- Make the runbook safe for on-call use without the stale-warning banner.
- Explain current service discovery commands for node-27 cron ingest, display
  API, public reverse proxy, active DB, and node-22 Slurm Gateway.
- Keep current production facts separate from the preserved two-node design
  intent in `two-node-deployment-overview.md`.
- Include verification commands that do not leak secrets.

**Non-Goals:**

- No runtime code, database schema, API, or frontend behavior changes.
- No change to node-22 Slurm scheduling code or compute wrappers.
- No attempt to rewrite `two-node-deployment-overview.md`; its banner already
  declares it design intent rather than current physical deployment.

## Decisions

- Treat node-27 as the physical writer/ingest host in this runbook, even though
  code-level `compute_control` remains the role contract. This matches
  `ROLE_BOUNDARY.md` and live evidence.
- Treat node-27 ingest as cron/on-demand bounded ingestion rather than a
  continuously running `plan-production` process. The observed active trigger is
  `*/10 * * * * /home/nwm/NWM/scripts/node27_autopipe_cron.sh`.
- Document display API host port as `8080`, because repo template,
  `scripts/ops/start-display-api.sh`, nginx proxy config, and corrected live
  deployment all converge on `127.0.0.1:8080`.
- Keep node-22 PostgreSQL `:55433` visible only as a historical do-not-connect
  note. Any current DB query examples must run on node-27 against `:55432`.

## Risks / Trade-offs

- Live process names may drift again. Mitigation: keep command recipes based on
  `ps`, `ss`, crontab, wrapper, and sanitized env checks rather than a single
  magic PID.
- Public display can fail when nginx target and uvicorn port diverge. Mitigation:
  runbook includes both local `/health` and public `/health` checks and names
  the port-alignment failure class.
- Some historical sections remain useful for troubleshooting old incidents.
  Mitigation: keep historical known-cardinality sections only when labeled as
  snapshots or stale examples, and prefer current check commands.
