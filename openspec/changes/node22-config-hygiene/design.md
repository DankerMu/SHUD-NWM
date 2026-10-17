# Design: node22-config-hygiene

## Context

node-22 runs the compute plane as user-scope systemd units under `/scratch/frd_muziyao/NWM` with the active 3.12.7
`.venv` (Python pin switch is #1831, not this change). Facts measured read-only on 2026-10-16:

- Deployed units: `nhms-compute-api.service` (loads `infra/env/compute.host.env`), `nhms-compute-scheduler.service` +
  `.timer` (loads `infra/env/compute.scheduler-dbfree.env`, drop-ins `10-slurm-gateway-token.conf` resetting
  `EnvironmentFile=` and adding the 0600 secret, `20-runtime-path.conf` setting `NHMS_PYTHON_VENV_BIN`),
  `nhms-slurm-gateway.service` (tracked generic template + drop-in `10-node22-live.conf`), and six tracked oneshots
  (`nhms-node22-refresh-timer-health`, `nhms-node22-scheduler-stall-health`, `nhms-scheduler-evidence-retention`,
  `nhms-scheduler-file-provider-refresh`, `nhms-scheduler-journal-retention`, `nhms-scheduler-state-index-capacity`),
  all byte-identical to `infra/systemd/`.
- Stale leftovers under `~/.config/systemd/user/`: `nhms-compute-scheduler.service.d/disabled/`, a
  `.service.pre-no-private-tmp-*` copy, and five backup directories from past rollouts.
- The stall probe (`scripts/node22_scheduler_stall_health.py`) classifies a pass from `submitted_count` and
  `blocked_candidate_count`; its docstring explicitly states that a `preflight_blocked` pass "is fully observed and
  counted by the scheduler itself". The scheduler evidence artifact carries `execution_boundary` (observed value
  `scheduler_root_preflight_blocked`) which the probe does not read.
- node-27 already has the generic `nhms-node27-unit-failure-alert@.service` + `scripts/node27_unit_failure_alert_once.sh`
  consumed through `OnFailure=` on six units; node-22 has nothing equivalent.
- `infra/env/README.md` §"node-22 unit → EnvironmentFile authority (#1694)" is the authority for which unit loads which
  file; its required-key block (line ~308) is the contract for `compute.scheduler-dbfree.env`.

## Goals / Non-Goals

**Goals:**
- A node-22 rebuild can start from `infra/systemd/node22/` instead of from a human's memory.
- Deployed-vs-tracked unit drift and a preflight-blocked scheduler are both non-zero exit codes on node-22.
- A failed node-22 oneshot unit sends mail.
- `infra/env/` on node-22 can be cleaned with a reviewed, dry-run-first script, and the repository stops describing the
  retired compose lane as the node-22 entry.

**Non-Goals:**
- Restoring `ghdc-data.mount` or any NFS work: the server is being upgraded (operator statement 2026-10-16, ~3 days).
- Deploying anything to node-22 inside this change: every deployment is an operator action with a receipt under
  `docs/runbooks/receipts/`, and no issue acceptance depends on it.
- The Python 3.11 switch of the node-22 `.venv` (#1831).
- Deleting the six git stashes or the 14 GB `.nhms-runs` on node-22 (listed in the audit; operator decision).
- Any change to scheduler, gateway or API runtime behaviour, to `compute.host.env` / `compute.scheduler-dbfree.env`
  values, or to the retired docker compose lane files themselves.
- node-27 units and alerts.

## Decisions

### D1 Tracked location and shape of the live units (user: track them)
Live units go under `infra/systemd/node22/` as exact copies of the deployed files, including the drop-in directories
(`nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf`, `20-runtime-path.conf`,
`nhms-slurm-gateway.service.d/10-node22-live.conf`). Secret files are referenced by path
(`/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env`) exactly as the deployed units do; no secret value is ever
tracked, and a test asserts no tracked unit under `node22/` contains `TOKEN=` or `PASS=` with a non-empty value.
Alternative rejected: rewriting the generic `nhms-slurm-gateway.service` template into the live unit, because other
deployments use the template defaults (`/opt/SHUD-NWM`, port 8081) on purpose.

### D2 Drift probe is read-only and separate from the stall probe
`scripts/node22_unit_drift_check.py` compares each file under `infra/systemd/node22/` with
`$XDG_CONFIG_HOME/systemd/user/<same relative path>` (default `~/.config/systemd/user`), reports `missing`,
`differs`, `same` per file and `untracked` for deployed `nhms-*` units with no tracked counterpart, writes a receipt
JSON, and exits 0 only when every tracked file is `same` and no `untracked` `nhms-*` unit exists. It never calls
`systemctl` and never writes under the systemd directory. It is a separate script, not a predicate inside the stall
probe, because the stall probe's contract is "one verdict per tick about the scheduler lane" and its thresholds are
tuned per pass shape; mixing in a file-diff verdict would change precedence semantics already covered by
`scheduler-stall-alerting`.

### D3 `root-blocked` pass shape in the stall probe (user: yes)
The classifier reads `execution_boundary` from the pass artifact. A value ending in `preflight_blocked` yields shape
`root-blocked` regardless of the counts. The streak rule is time-based like the existing no-submission rule: when the
newest pass is `root-blocked` and the span from the oldest consecutive `root-blocked` pass to the newest exceeds
`NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES` (default 60; one hour is twelve 5-minute ticks, well above a single
transient refusal), the verdict is `scheduler_root_blocked` (exit 1, runbook section `stall-scheduler-root-blocked`).
Precedence: below `probe_failed`, `timer_not_enabled`, `timer_stopped`, `scheduler_service_failed` (a lane that is dead
outranks a lane that runs but refuses) and above `submission_stalled` and `no_progress`. A `root-blocked` pass is
neither neutral nor idle for the existing streaks: it does not reset the no-submission streak (it submitted nothing by
construction) and it is not counted toward it (the condition is reported by its own verdict), mirroring how
`resource_limit_blocked` is handled today. The one-hour default is a checked-in constant, overridable only through the
documented env var.

### D4 node-22 unit-failure alert mirrors node-27, with its own env file
`infra/systemd/nhms-node22-unit-failure-alert@.service` and `scripts/node22_unit_failure_alert_once.sh` copy the node-27
pair (same `%n`/`%i` instance convention, same bounded 30-line journal read, same `TimeoutStartSec=120`), with
`WorkingDirectory=/scratch/frd_muziyao/NWM` and `EnvironmentFile=-%h/NWM/infra/env/node22-unit-failure-alert.env`
whose tracked example `infra/env/node22-unit-failure-alert.example` carries the same variable names as the node-27
frontier-alert lane (`NHMS_ALERT_EMAIL_TO`, `NHMS_ALERT_EMAIL_FROM`, `NHMS_FRONTIER_SENDMAIL`, SMTP keys). Unconfigured
recipient exits 0, as on node-27. The six tracked node-22 oneshot services get one
`OnFailure=nhms-node22-unit-failure-alert@%n.service` line each. The scheduler and gateway units (D1) are long-running
or timer-driven with `Restart=`; they are not wired in this change (Open question Q1).

### D5 Env hygiene is repository-side plus a dry-run sweep script (user: script included)
- `.gitignore` gains `.nhms-work/` next to the existing `.nhms-*` entries.
- `docs/runbooks/two-node-deployment-overview.md` §5.2 gets the DOC_STATUS section marker (`status: superseded`,
  `current_authority: infra/env/README.md#node-22-unit--environmentfile-authority-1694`, `superseded_by:
  docs/runbooks/node22-control-plane-manual-recovery.md`) before the compose-lane list, and one sentence stating that
  `infra/env/compute.env` is a retired docker-e2e artifact no unit loads.
- `infra/env/README.md` gets a "Backup copies" section: backups never live in `infra/env/`; they go to
  `~/NWM-presync-backup-<UTC date>/infra-env/`, the same root the ff-only sync runbook already uses; and the required-key
  block for `compute.scheduler-dbfree.env.example` states, for each of the seven keys the live file lacks
  (`NHMS_SCHEDULER_LEASE_GUARD_MODE`, `NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS`,
  `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT`, `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`,
  `NHMS_SCHEDULER_TIMING_LEVEL`, `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS`, `NHMS_SCHEDULER_EVIDENCE_MAX_MB`), whether it
  is required or optional-with-code-default, by reading the consuming code's default; required ones become an operator
  step in `node22-control-plane-manual-recovery.md`.
- `scripts/ops/node22_env_backup_sweep.sh`: dry-run by default, `--apply` to execute; candidates are files directly under
  `infra/env/` matching `*.bak*`, `*.backup-*`, `*.pre-*`; it refuses any candidate that appears in an `EnvironmentFile=`
  line of any file under `~/.config/systemd/user/` (fail closed, exit 2 with the offending unit named); it prints the
  plan as one line per file and writes a receipt; it moves with `mv` into the dated directory and never deletes.

### D6 Verification routing
Everything in this change is verified by local pytest (`tests/test_node22_scheduler_stall_health.py`, new
`tests/test_node22_unit_drift_check.py`, `tests/test_node22_env_backup_sweep.py`, `tests/test_node22_unit_tracking.py`)
plus `uv run ruff check .`, the governance docs gate (`uv run pytest -q tests/test_entropy_audit_report_contract.py`)
for the runbook marker, and `systemd-analyze --user verify` run locally on the tracked unit files where a Linux CI
runner is available. node-22 live receipts are deferred until the server upgrade ends and are operator actions.

## Sketch seams under test

- `scripts/node22_scheduler_stall_health.py` CLI: receipt JSON + exit code over a synthetic evidence root. Reason:
  it is the existing, highest seam the alerting lane already tests; D3 is a new shape inside it.
- `scripts/node22_unit_drift_check.py` CLI: receipt + exit code over two temp directories. Reason: the whole feature is
  that exit code.
- `scripts/ops/node22_env_backup_sweep.sh` CLI: dry-run plan lines, refusal exit 2, `--apply` move, over a temp tree.
  Reason: the operator runs exactly this.
- `infra/systemd/node22/**` as data: a pytest asserting no secret values and that every `EnvironmentFile=` path is one
  the `infra/env/README.md` authority table names. Reason: the tracked copies are the deliverable; the test keeps them
  from rotting.

## Risks / Trade-offs

- The `root-blocked` default of 60 minutes could mask a short real outage; the trade is against false alerts on the
  single transient refusal the runtime already retries. Overridable via env.
- Tracked copies of live units will drift again if operators edit on the host; D2's probe is the detector, and the
  recovery runbook gains the step "run the drift probe before and after any unit edit".
- The sweep script's refusal list is built from `EnvironmentFile=` lines; a backup referenced by a stale drop-in under
  `disabled/` would also be protected. That is the safe direction.

## Migration Plan

Repository-only. Operator deployment, after the upgrade window: install the alert template and env file, `daemon-reload`,
run the drift probe (expect `same` for every tracked unit), run the sweep in dry-run, then `--apply`, record receipts.

## Open Questions

- Q1: whether `nhms-compute-scheduler.service` (timer-driven, `Restart=` semantics) should also carry `OnFailure=`; the
  stall probe already covers its failure, so a second mail per failed pass may be noise. Decide after one week of alert
  volume.

## Not yet specified

- Whether the five rollout backup directories under `~/.config/systemd/user/` and the `disabled/` drop-in directory
  should be swept by the same script; the problem is not precisely stated yet (which of them are still rollback
  targets is unknown).

## Grill ledger (2026-10-16)

- [decided:user] change split -> two changes; this one owns infra/ops with node-22 oracle.
- [decided:user] NFS incident -> no issue; server upgrade in progress (~3 days). Stall-probe blind spot stays in scope as a control gap (D3).
- [decided:user] track live units + probe predicates + OnFailure mirror -> yes (D1-D4); deployment is operator action with receipt.
- [decided:user] env cleanup -> repository docs/policy plus a dry-run sweep script (D5).
- [decided:fact-check] `preflight_blocked` passes are read as idle by the current probe (docstring at the classifier, counts both zero in the observed artifacts).
- [open] Q1 OnFailure on the scheduler unit.
