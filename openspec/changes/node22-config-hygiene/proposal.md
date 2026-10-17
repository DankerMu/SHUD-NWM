# Proposal: node22-config-hygiene

## Why

The 2026-10-16 repo entropy audit (`docs/governance/entropy-audit-2026-10-16.md` §0 and §4) found that the node-22
compute plane's configuration has drifted away from the repository and that its own health probes cannot see the
failure mode it is currently in:

- The three production units that actually run the compute plane (`nhms-compute-api.service`,
  `nhms-compute-scheduler.service`, `nhms-compute-scheduler.timer`) plus the live drop-ins exist only under
  `~/.config/systemd/user/` on node-22. `infra/systemd/` tracks a generic `nhms-slurm-gateway.service` that differs from
  the deployed one. A rebuild of node-22 cannot be reproduced from the repository.
- After the 2026-10-13 reboot `ghdc-data.mount` failed, every scheduler pass since ends
  `execution_boundary=scheduler_root_preflight_blocked` with zero candidates, and four timers fail on every tick. The
  stall probe `nhms-node22-scheduler-stall-health` keeps answering `verdict=ok` because a preflight-blocked pass carries
  `submitted_count=0` and `blocked_candidate_count=0`, which the probe reads as idle. There is no `OnFailure=` alerting
  on node-22 at all, so the four failed units were silent for three days. (The mount itself is a server upgrade in
  progress and is out of scope.)
- `infra/env/` on node-22 holds 57 backup copies (`*.bak-*`, `*.pre-*`, `*.backup-*`) beside the live files, an orphan
  June docker-e2e `compute.env` that no unit loads but that `docs/runbooks/two-node-deployment-overview.md` §5.2 still
  names as the node-22 entry, and a live `compute.scheduler-dbfree.env` missing seven keys its tracked example lists.
  `.nhms-work/` is not gitignored and shows 25 untracked files in `git status`, which is the gate the ff-only sync
  runbook relies on.

## What Changes

- Track the live node-22 user-scope units and drop-ins under `infra/systemd/node22/` (secret paths referenced, never
  secret values) and add a read-only drift probe that diffs deployed units against the tracked copies.
- Teach the stall probe a new pass shape, `root-blocked` (a pass whose `execution_boundary` ends in
  `preflight_blocked`), with its own time-based verdict and runbook section, so a scheduler that is preflight-blocked for
  longer than the threshold is a non-zero unit.
- Add a node-22 `OnFailure=` unit-failure alert template and handler mirroring the node-27 one, and wire the six tracked
  node-22 oneshot units to it.
- Env hygiene: gitignore `.nhms-work/`; mark the compose-lane entry in `two-node-deployment-overview.md` §5.2 as
  superseded with the DOC_STATUS marker; add a backup-placement policy to `infra/env/README.md`; reconcile the
  `compute.scheduler-dbfree.env.example` required-key block with the observed live drift; ship a dry-run-by-default
  backup sweep script that moves backup copies out of `infra/env/` into `~/NWM-presync-backup-<date>/`.

## Capabilities

### New Capabilities
- `node22-unit-tracking`: the live node-22 units are a tracked artifact and a read-only probe reports deployed-vs-tracked drift.
- `node22-unit-failure-alert`: node-22 oneshot units that fail become a mail alert through a generic `OnFailure=` template.
- `node22-env-hygiene`: env backup placement, retired compose-lane marker, example/live key reconciliation, `.nhms-work/` ignore.

### Modified Capabilities
- `scheduler-stall-alerting`: the no-submission streak classifier gains the `root-blocked` pass shape and a
  `scheduler_root_blocked` verdict; a preflight-blocked scheduler is no longer graded idle.

## Impact

- `infra/systemd/` (new `node22/` subtree, `nhms-node22-unit-failure-alert@.service`, `OnFailure=` lines on six units),
  `infra/env/README.md`, `infra/env/compute.scheduler-dbfree.env.example`, `.gitignore`.
- `scripts/node22_scheduler_stall_health.py` and its tests; new `scripts/node22_unit_drift_check.py`,
  `scripts/node22_unit_failure_alert_once.sh`, `scripts/ops/node22_env_backup_sweep.sh` with tests.
- `docs/runbooks/two-node-deployment-overview.md`, `docs/runbooks/production-ops/stuck-detection.md`,
  `docs/runbooks/node22-control-plane-manual-recovery.md`.
- No runtime behaviour of the scheduler, gateway or API changes. Deploying the tracked units, the alert template or the
  sweep on node-22 remains an operator action with a receipt and is not part of any issue's acceptance.
