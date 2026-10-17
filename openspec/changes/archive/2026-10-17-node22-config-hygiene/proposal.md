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
  `execution_boundary=scheduler_root_preflight_blocked` with zero candidates, and four timer-driven services fail on
  every tick. The stall probe `nhms-node22-scheduler-stall-health` keeps answering `verdict=ok` because that pass
  artifact is written before the progress guard exists, so the probe classifies it as `neutral` and skips it; nothing
  in the probe reads `execution_boundary`. There is no `OnFailure=` alerting
  on node-22 at all, so the four failed units were silent for three days. (The mount itself is a server upgrade in
  progress and is out of scope.)
- `infra/env/` on node-22 holds 57 backup copies (`*.bak-*`, `*.pre-*`, `*.backup-*`) beside the live files, an orphan
  June docker-e2e `compute.env` that no unit loads but that `docs/runbooks/two-node-deployment-overview.md` §5.2 still
  names as the node-22 entry, and a live `compute.scheduler-dbfree.env` missing seven keys its tracked example lists.
  `.nhms-work/` is not gitignored and shows 25 untracked files in `git status`, which is the gate the ff-only sync
  runbook relies on.

## Triage (subagent-workflow fixture)

Issue type: refactor (configuration tracking, probes, env hygiene; no scheduler/gateway/API runtime change)
Fixture level: per task, as declared in `tasks.md` (1.1 compact, 1.2 compact, 2.1 expanded, 3.1 compact, 3.2 none, 4.1 none, 4.2 compact, 4.3 compact, 4.4 compact); `design.md` exists because 2.1 is expanded
Upstream suggested level: same values written by `stage-change-pipeline` into #2910-#2918 (agree)
Fixture level note for 1.1: stays compact although it selects the schema / secrets / config / legacy packs — the manifest format's only consumers are inside this change (1.2, 3.1, 3.2), the tracked files are data with no runtime path, and design D1 already names must-preserve behaviour, consumers and seams; the sibling-surface check that expanded would add was done in the fixture review (selector routing for the test's literal readers, now pinned in tasks.md 1.1).
Fixture level note for 1.2: stays compact although it adds a CLI with file IO — its only consumer is an operator running it by hand on node-22 (no timer, no automated reader of its receipt, no shared entrypoint), it is read-only toward the systemd directory, and the full CLI/receipt/exit contract is pinned in tasks.md 1.2 (design D2 holds the scan scope and exit matrix).
Blast radius: a wrong tracked copy or manifest makes the drift probe (1.2) report drift against the real node-22 or hide it; a wrong stall predicate (2.1) mails on healthy passes or stays silent on a root-blocked scheduler; a wrong sweep (4.4) moves a live env file. Nothing here changes what the scheduler, gateway or API execute.
Selected risk packs: see "Risk packs" in `tasks.md`
Evidence floor: per task `Verify` line in `tasks.md` (local `uv run ruff check .` + the listed pytest files; no node-22 / node-27 oracle, design D6)

## What Changes

- Track the live node-22 user-scope units, the gateway base unit and the drop-ins under `infra/systemd/node22/`
  (secret paths referenced, never secret values; byte source is the read-only receipt
  `docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md`), with a manifest, and add a read-only drift probe
  that diffs every manifest entry against the deployed file and lists untracked `nhms-*` units and drop-ins.
- Teach the stall probe a root-blocked run predicate that reads `execution_boundary` directly (exact value
  `scheduler_root_preflight_blocked`; the pass stays `neutral` for every existing streak), with its own time-based
  verdict `scheduler_root_blocked` and runbook section, so a scheduler that is root-blocked for at least the threshold is
  a non-zero unit.
- Add a node-22 `OnFailure=` unit-failure alert template and handler mirroring the node-27 one (absolute node-22 paths,
  an explicit env key set without `DATABASE_URL`, local `sendmail` as the default transport), and wire the six tracked
  node-22 oneshot units to it.
- Env hygiene: gitignore `.nhms-work/`; mark the compose-lane entry in `two-node-deployment-overview.md` §5.2 as
  superseded with the DOC_STATUS marker; add a backup-placement policy to `infra/env/README.md`; reconcile the
  seven keys the live `compute.scheduler-dbfree.env` lacks in a classification table beside the required-key block
  (required / optional with the loader's effective default / optional but example differs); ship a dry-run-by-default
  backup sweep script that moves backup copies (`*.bak*`, `*.backup-*`, `*.pre-*`) out of `infra/env/` into
  `~/NWM-presync-backup-<date>/infra-env/`.

## Capabilities

### New Capabilities
- `node22-unit-tracking`: the live node-22 units are a tracked, manifest-listed artifact and a read-only probe reports deployed-vs-tracked drift including untracked drop-ins.
- `node22-unit-failure-alert`: node-22 oneshot units that fail become a mail alert through a generic `OnFailure=` template.
- `node22-env-hygiene`: env backup placement, retired compose-lane marker, example/live key reconciliation, `.nhms-work/` ignore.

### Modified Capabilities
- `scheduler-stall-alerting`: a root-blocked run predicate and the `scheduler_root_blocked` verdict are added; the pass
  shape taxonomy and the neutral definition are modified to state that a root-blocked pass stays neutral for every
  existing streak while the new predicate reads it.

## Impact

- `infra/systemd/` (new `node22/` subtree with `MANIFEST.json`, `nhms-node22-unit-failure-alert@.service`, `OnFailure=`
  lines on six units), `infra/env/README.md`, `infra/env/node22-unit-failure-alert.example`, `.gitignore`,
  `scripts/select_ci_tests.py` (owner rules for the new files).
- `scripts/node22_scheduler_stall_health.py` and its tests; new `scripts/node22_unit_drift_check.py`,
  `scripts/node22_unit_failure_alert_once.sh`, `scripts/ops/node22_env_backup_sweep.sh` with tests.
- `docs/runbooks/two-node-deployment-overview.md`, `docs/runbooks/production-ops/stuck-detection.md`,
  `docs/runbooks/node22-control-plane-manual-recovery.md`.
- No runtime behaviour of the scheduler, gateway or API changes. Deploying the tracked units, the alert template or the
  sweep on node-22 remains an operator action with a receipt and is not part of any issue's acceptance.
