# Design: node22-config-hygiene

## Context

node-22 runs the compute plane as user-scope systemd units under `/scratch/frd_muziyao/NWM` with the active 3.12.7
`.venv` (Python pin switch is #1831, not this change). Facts measured read-only on 2026-10-16 (receipt
`docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md` holds the seven unit texts with sha256):

- Deployed units: `nhms-compute-api.service` (loads `infra/env/compute.host.env`), `nhms-compute-scheduler.service` +
  `.timer` (loads `infra/env/compute.scheduler-dbfree.env`; drop-in `10-slurm-gateway-token.conf` emits an empty
  `EnvironmentFile=` reset line, then the base env, then the 0600 secret; `20-runtime-path.conf` sets
  `NHMS_PYTHON_VENV_BIN`), `nhms-slurm-gateway.service` whose deployed base unit differs from the tracked generic
  template (`WorkingDirectory=/scratch/...`, `EnvironmentFile=.../compute.host.env`, `NHMS_SERVICE_ROLE=slurm_gateway`,
  port 8090, `SLURM_GATEWAY_SLURM_BIN_PATH=/usr/bin`) plus drop-in `10-node22-live.conf`, and six tracked oneshots
  (`nhms-node22-refresh-timer-health`, `nhms-node22-scheduler-stall-health`, `nhms-scheduler-evidence-retention`,
  `nhms-scheduler-file-provider-refresh`, `nhms-scheduler-journal-retention`, `nhms-scheduler-state-index-capacity`),
  all byte-identical to `infra/systemd/`.
- `%h` on node-22 is `/users/frd_muziyao/`; `/users/frd_muziyao/NWM` does not exist. Every deployed node-22 unit uses
  absolute `/scratch/frd_muziyao/NWM/...` paths. `/usr/sbin/sendmail` and `/usr/bin/mailx` exist on node-22; whether
  the local MTA delivers off-host is not known (operator receipt at deployment).
- Stale leftovers under `~/.config/systemd/user/`: `nhms-compute-scheduler.service.d/disabled/`, a
  `.service.pre-no-private-tmp-*` copy, and five backup directories from past rollouts.
- Stall probe (`scripts/node22_scheduler_stall_health.py`): `classify_pass` returns `neutral` for any artifact without
  a `progress_guard` block. The DB-free pre-lock path (`services/orchestrator/scheduler_runtime.py` around `:643-660`)
  writes `scheduler_root_preflight_blocked` artifacts without that block, so every pass since 2026-10-13 is `neutral`
  and skipped; the probe never reads `execution_boundary`. `grade()` has eleven ordered verdict slots:
  `probe_failed`, `timer_not_enabled`, `timer_stopped`, `scheduler_service_failed`, `scheduler_not_triggering`,
  `evidence_unavailable`, `evidence_stale`, `pass_limit_blocked`, `lock_contended_persistent`, `submission_stalled`,
  `no_progress_circuit_open`, then `ok`. Time gates compare with `>=` and `load_config` rejects unreachable thresholds
  against `(scan_limit - HOUR_BUCKET_MARGIN - 1) * MIN_INTER_PASS_MINUTES`. Other `preflight_blocked` boundaries
  (`db_free_runtime_preflight_blocked`, `evidence_preflight_blocked`, `slurm_preflight_blocked`, bare
  `preflight_blocked`) exist; the last three are post-candidate passes with a progress guard and are already counted by
  the existing blocked streak.
- node-27 has `nhms-node27-unit-failure-alert@.service` + `scripts/node27_unit_failure_alert_once.sh` consumed through
  `OnFailure=` on five user-scope units; node-22 has nothing equivalent.
- `infra/env/README.md` §"node-22 unit → EnvironmentFile authority (#1694)" is the authority for which unit loads which
  file (table paths are written relative to `/scratch/frd_muziyao/`, as `NWM/infra/env/...`); its required-key block is
  parsed by `tests/test_env_templates.py` and accepts only `KEY`, `KEY=value` or full-line `#` comments, where a bare
  `KEY` means "must be present". All seven keys the live file lacks are already present in the example; the gap is in
  the live file, not the example.

## Goals / Non-Goals

**Goals:**
- A node-22 rebuild can start from `infra/systemd/node22/` instead of from a human's memory.
- Deployed-vs-tracked unit drift (including drop-ins and the top-level units) and a root-blocked scheduler are both
  non-zero exit codes on node-22.
- A failed node-22 oneshot unit sends mail, and a broken mail channel is a non-zero handler, not a silent skip.
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
- Treating `db_free_runtime_preflight_blocked` or the post-candidate `*_preflight_blocked` boundaries as root-blocked:
  their remedy is not a mount check; they keep today's classification.
- Alert de-duplication or rate limiting (same as node-27: silence comes from clearing the condition).
- node-27 units and alerts.

## Decisions

### D1 Tracked location, manifest and shape of the live units (user: track them)
Live units go under `infra/systemd/node22/` as byte-exact copies of the seven deployed files in the receipt:
`nhms-compute-api.service`, `nhms-compute-scheduler.service`, `nhms-compute-scheduler.timer`,
`nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf`, `nhms-compute-scheduler.service.d/20-runtime-path.conf`,
`nhms-slurm-gateway.service` (the deployed base unit, distinct from the generic template at `infra/systemd/`) and
`nhms-slurm-gateway.service.d/10-node22-live.conf`. `infra/systemd/node22/MANIFEST.json` lists every file deployed on
node-22 as `{deployed_relative_path, tracked_path, sha256}`: the seven above (tracked under `node22/`), the twelve
top-level oneshot `.service`/`.timer` files (tracked at `infra/systemd/<name>`) and, once D4 lands, the alert template
(twenty entries), so the drift probe has one explicit comparison set, the top-level oneshots are byte-compared too, and
nothing this change deploys is `untracked` to its own probe. Secret files are referenced by path only
(`/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env`); a test asserts no tracked file under `node22/` assigns a
non-empty value to a `*TOKEN`, `*SECRET`, `*PASS`, `*PASSWORD` variable, that every non-empty `EnvironmentFile=` value
(after stripping a leading `-` and resolving `NWM/...` table entries against `/scratch/frd_muziyao/`) is a README
authority-table path or the documented secret path, that empty `EnvironmentFile=` reset lines are allowed, and that
each `node22/` file's sha256 equals the manifest and the receipt. Alternative rejected: rewriting the generic
`nhms-slurm-gateway.service` template into the live unit, because other deployments use the template defaults on
purpose.

### D2 Drift probe is manifest-driven, read-only and separate from the stall probe
`scripts/node22_unit_drift_check.py` reads the manifest, compares each entry's deployed file
(`--systemd-user-dir`, default `$XDG_CONFIG_HOME/systemd/user` or `~/.config/systemd/user`) with the tracked file by
bytes, reports `same` / `differs` (unified diff on stdout) / `missing`, then scans only the systemd directory's top level
(`nhms-*.service`, `nhms-*.timer`) and one level of `nhms-*.service.d/*.conf` for files absent from the manifest and
reports them as `untracked`; backup directories, `*.wants/` links and anything deeper are ignored by construction
(the fog item about those directories stays unspecified). Receipt JSON `nhms.node22.unit_drift_check.v1` carries
per-file status, timestamps and the manifest sha. Exit matrix: 0 when every manifest entry is `same` and `untracked` is
empty; 1 when any entry is `differs` or `missing` or `untracked` is non-empty; 2 when the manifest is missing or empty,
the systemd directory does not exist, a tracked or deployed file is unreadable, or the receipt cannot be written.
It never calls `systemctl` and never writes under the systemd directory. It is a separate script because the stall
probe's contract is one verdict per tick about the scheduler lane.

### D3 Root-blocked run predicate in the stall probe (user: yes; reviewer fact-check: passes are neutral, not idle)
No new pass shape. `PassRecord` gains `execution_boundary: str | None` read from the artifact. A new run predicate
`root_blocked_run(records, window)` walks the newest-first window and returns the contiguous run of passes whose
`execution_boundary == "scheduler_root_preflight_blocked"` (exact match; any other pass ends the run), with its pass
count and the minutes from the oldest run member's recorded time to the newest. The shape of those passes stays
`neutral`, so `blocked_streak`, `in_flight_run`, `frontier_pass` and the receipt `shapes` counts are unchanged. Verdict
`scheduler_root_blocked` fires when the newest pass is in the run and `span_minutes >= root_blocked_minutes`; it is
graded immediately after `lock_contended_persistent` and before `submission_stalled` (slot 10 of 13: twelve non-ok verdicts plus `ok`), exit 1, runbook
section `stall-scheduler-root-blocked`. Threshold env `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES`, default 60; `load_config`
rejects values below `2 * MIN_INTER_PASS_MINUTES` (10) and above the same reachable bound the in-flight gate uses.
Receipt gains `root_blocked_run: {passes, span_minutes, threshold_minutes, newest_is_root_blocked}` so the verdict can
be recomputed from the receipt. Alternative rejected: a new non-neutral shape, because it would break the in-flight
time gate and frontier selection (`#2655` freeze shape) and the closed `shapes` dictionary.

Sibling surfaces (D3): the writer has more guard-less `preflight_blocked` boundaries than the root one, and every one
of them reads as neutral in `classify_pass` and ends the root-blocked run: `db_free_runtime_preflight_blocked`
(`scheduler_runtime.py` ~:670-709, pre-lock, same `_write_prelock_blocked_evidence` shape; it checks the `/ghdc`
registry/index/copyback roots the root preflight does not), `scheduler_rollback_fence_prepared` (~:758-793) and
`db_free_registry_blocked` (~:895-925, post-lock, pre-candidate, guard constructed but not written).
`scheduler_timing_level_unrecognised` (~:597-619) is also pre-lock and guard-less but its artifact never lands (the
write helper returns `None` for its payload). The post-candidate boundaries (`evidence_preflight_blocked`,
`slurm_preflight_blocked`, bare `preflight_blocked`) carry the guard and are counted by the blocked streak. Residual
risk, deliberately out of this change's scope (spec pins that `db_free_runtime_preflight_blocked` ends the run): a lane
that sits on one of the other guard-less boundaries still grades `ok` with `root_blocked_run.passes == 0`; the runbook
therefore tells the operator to confirm recovery by the newest artifact's `execution_boundary`, not by the verdict, and
widening the predicate is a follow-up issue. The
`lock_contended_persistent`-over-root-blocked pairing is structurally unreachable (both predicates start at the newest
pass, which cannot be both lock-contended and root-blocked), so it has no test; the buildable precedence cases are
`timer_stopped`, `pass_limit_blocked` (outrank) and `submission_stalled` (outranked). The predicate walks
`records[:streak_window]`, which is what makes "the same reachable bound the in-flight gate uses" the correct upper
bound. Observer/observed parity: the probe carries the literal `scheduler_root_preflight_blocked`; a parity test reads
the writer source (`services/orchestrator/scheduler_runtime.py`) and asserts the literal, routed by a second node id on
the writer's selector stop rule (same pattern as `test_e5`).

Review focus (D3): (1) test-harness reachability — `BASE_CONFIG` (`SCAN_LIMIT=16`) admits at most 15 minutes, so the
suite must set the new env to 10 and the 60-minute scenario runs under `SHIPPED_CONFIG`; (2) the walked window is
`streak_window`, not `scan_limit`; (3) the new `load_config` check sits after the in-flight check so existing exact
stderr assertions (c1, c7, i17) see their own message first; (4) the parity edge above; (5) existing verdict
expectations unchanged with the root-blocked pass inserted (only `neutral_count`, `no_submission_neutral_skipped` and
the new block differ).

### D4 node-22 unit-failure alert mirrors node-27, with absolute paths and an explicit env key set
`infra/systemd/nhms-node22-unit-failure-alert@.service` (user scope, `Type=oneshot`,
`WorkingDirectory=/scratch/frd_muziyao/NWM`,
`EnvironmentFile=-/scratch/frd_muziyao/NWM/infra/env/node22-unit-failure-alert.env`,
`ExecStart=/scratch/frd_muziyao/NWM/scripts/node22_unit_failure_alert_once.sh %i`, `TimeoutStartSec=120`) and
`scripts/node22_unit_failure_alert_once.sh` copy the node-27 pair's `%n`/`%i` convention, bounded 30-line journal read
and exit discipline: unconfigured recipient exits 0 after logging `SKIPPED`; a configured transport that fails exits
non-zero. The tracked example `infra/env/node22-unit-failure-alert.example` carries exactly `NHMS_ALERT_EMAIL_TO`,
`NHMS_ALERT_EMAIL_FROM`, `NHMS_FRONTIER_SENDMAIL`, `NHMS_SMTP_HOST`, `NHMS_SMTP_PORT`, `NHMS_SMTP_USER`,
`NHMS_SMTP_PASS` and nothing else (no `DATABASE_URL`, no `NHMS_FRONTIER_*` state keys: node-22 is DB-free).
`NHMS_FRONTIER_SENDMAIL` defaults in the example to `/usr/sbin/sendmail` (present on node-22); the SMTP shim is an
operator alternative and, if used, must be invoked through `/scratch/frd_muziyao/NWM/.venv/bin/python`, never
`#!/usr/bin/env python3`. The README authority table gains a row for the alert env file. The six tracked node-22
oneshot services get one `OnFailure=nhms-node22-unit-failure-alert@%n.service` line each; a wiring test asserts the
line on exactly those six and asserts no node-22 unit contains `%h/NWM`. The scheduler, API and gateway units are not
wired (Open question Q1). Off-host mail delivery from node-22 is verified only by the deployment receipt.

### D5 Env hygiene is repository-side plus a dry-run sweep script (user: script included)
- `.gitignore` gains `.nhms-work/` next to the existing `.nhms-*` entries.
- `docs/runbooks/two-node-deployment-overview.md` §5.2: a complete DOC_STATUS section marker (`status: superseded`,
  `current_authority: infra/env/README.md#node-22-unit--environmentfile-authority-1694`, `superseded_by:
  docs/runbooks/node22-control-plane-manual-recovery.md`, `status_since: 2026-10-16`, `archive_scope: section`,
  `retained_for: compose-lane design evidence`) is inserted immediately before the compose-lane list, which stays
  inside the marked section; one sentence above the marker states that `infra/env/compute.env` is a retired docker-e2e
  artifact no node-22 unit loads and that the unit-to-env authority is `infra/env/README.md`. A test parses the file
  with `scripts/governance/entropy_audit/archive_status.py::_section_archive_status_marker_ranges` and asserts one
  complete `section` range covering the list.
- `infra/env/README.md` gains, outside the required-key block, a "compute.scheduler-dbfree.env live-drift
  classification (2026-10-16)" table with columns key / status / effective value when unset / operator action, status
  one of `required`, `optional (effective default: <value>)`, `optional, example differs (example: <v>, default:
  <v>)`. "Effective value when unset" means the value the scheduler config loader applies when only that key is
  absent, including legacy fallbacks (e.g. `NHMS_SCHEDULER_LEASE_GUARD_MODE` falls back to
  `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE`, then `flock`). `tests/test_node22_scheduler_env_drift_table.py` parses the
  table, asserts every key is in the example, and asserts each stated effective default by calling the loader with that
  key removed; `tests/test_env_templates.py` keeps passing because the block is untouched. `required` rows get an
  operator step in `node22-control-plane-manual-recovery.md`.
- "Backup copies" policy section in `infra/env/README.md`: backups never live under `infra/env/`; they belong under
  `~/NWM-presync-backup-<UTC date>/infra-env/`.
- `scripts/ops/node22_env_backup_sweep.sh`: dry-run by default, `--apply` to execute; options `--env-dir`,
  `--systemd-user-dir`, `--backup-root`, `--home` (for `%h` expansion); candidates are regular files directly under the
  env dir matching `*.bak*`, `*.backup-*`, `*.pre-*`; the refusal set is every `EnvironmentFile=` value in top-level
  `nhms-*` units and one level of `nhms-*.service.d/*.conf`, normalized by stripping a leading `-`, expanding `%h` with
  `--home`, and `realpath -m` on both sides; any candidate in the refusal set exits 2 naming the unit before any move;
  plan is one line per candidate; receipt `<backup root>/infra-env/sweep-receipt-<UTC>.json`
  (`nhms.node22.env_backup_sweep.v1`); `--apply` moves with `mv -n` and exits 2 if a target already exists; nothing is
  ever deleted.

### D6 Verification routing
Everything in this change is verified by local pytest plus `uv run ruff check .`: `tests/test_node22_unit_tracking.py`,
`tests/test_node22_unit_drift_check.py`, `tests/test_node22_scheduler_stall_health.py`,
`tests/test_node22_unit_failure_alert.py`, `tests/test_gitignore_nhms_work.py`,
`tests/test_node22_runbook_section_markers.py`, `tests/test_node22_scheduler_env_drift_table.py`,
`tests/test_env_templates.py`, `tests/test_node22_env_backup_sweep.py`, and the governance docs gate
`tests/test_entropy_audit_report_contract.py` for runbook edits. New files under `infra/systemd/node22/`,
`infra/systemd/nhms-node22-unit-failure-alert@.service`, `infra/env/node22-unit-failure-alert.example` and the two new
scripts get `PathTestRule` rows in `scripts/select_ci_tests.py` so PR CI runs their tests instead of collect-only; every
task that edits the selector also runs `tests/test_select_ci_tests.py`.
`systemd-analyze --user verify` is not available locally and is part of the operator deployment receipt, not of any
task's acceptance. node-22 live receipts are deferred until the server upgrade ends.

## Sketch seams under test

- `scripts/node22_scheduler_stall_health.py` CLI: receipt JSON + exit code over a synthetic evidence root whose
  root-blocked fixtures carry **no** `progress_guard` (the real shape). Reason: the existing highest seam of the alerting
  lane; D3 adds one predicate inside it.
- `scripts/node22_unit_drift_check.py` CLI: receipt + exit code over a manifest and two temp directories. Reason: the
  whole feature is that exit code.
- `scripts/node22_unit_failure_alert_once.sh` with a fake sendmail shim: one message, skip path, failing transport.
  Reason: the handler's three exits are its contract.
- `scripts/ops/node22_env_backup_sweep.sh` CLI: dry-run plan lines, refusal exit 2, `--apply` move, over a temp tree.
  Reason: the operator runs exactly this.
- `infra/systemd/node22/**` + `MANIFEST.json` as data: sha256 vs receipt, secret-value and EnvironmentFile checks.
  Reason: the tracked copies are the deliverable; the test keeps them from rotting.

## Risks / Trade-offs

- The 60-minute default could mask a short real outage; the trade is against false alerts on a transient refusal the
  runtime already retries. Overridable via env within the validated range.
- Tracked copies will drift again if operators edit on the host; D2's probe is the detector, and the recovery runbook
  gains the step "run the drift probe before and after any unit edit".
- With `OnFailure=` on the stall probe, a persistent verdict mails every 15 minutes (no de-duplication, same as
  node-27); silence comes from clearing the condition. Deployment happens after the upgrade window, so the current
  outage will not produce a mail storm.
- If the evidence root itself were on a blocked root the pre-lock artifact would not be written and the new verdict would
  be unreachable; today the evidence root is on `/scratch`, and `evidence_stale` remains the fallback.

## Migration Plan

Repository-only. Operator deployment, after the upgrade window: install the alert template, its env file and the six
`OnFailure=`-wired oneshot units from the merged tree, `daemon-reload`, run `systemd-analyze --user verify` on the
instantiated template, run the drift probe (expect every manifest entry `same`, no `untracked`), run the sweep in
dry-run, then `--apply`, send one test mail through the configured transport, record receipts.

## Open Questions

- Q1: whether `nhms-compute-scheduler.service` (timer-driven, `Restart=` semantics) should also carry `OnFailure=`; the
  stall probe already covers its failure, so a second mail per failed pass may be noise. Decide after one week of alert
  volume.

## Not yet specified

- Whether the five rollout backup directories under `~/.config/systemd/user/` and the `disabled/` drop-in directory
  should be swept by the same script; which of them are still rollback targets is unknown, so the problem is not
  precisely stated.

## Grill ledger (2026-10-16)

- [decided:user] change split -> two changes; this one owns infra/ops with node-22 oracle.
- [decided:user] NFS incident -> no issue; server upgrade in progress (~3 days). Stall-probe blind spot stays in scope as a control gap (D3).
- [decided:user] track live units + probe predicates + OnFailure mirror -> yes (D1-D4); deployment is operator action with receipt.
- [decided:user] env cleanup -> repository docs/policy plus a dry-run sweep script (D5).
- [decided:fact-check] the production root-blocked artifact has no `progress_guard`, so the probe classifies it `neutral` and skips it; nothing reads `execution_boundary` (corrected from "idle" by Stage 3 review).
- [decided:fact-check] `%h/NWM` does not exist on node-22; `/usr/sbin/sendmail` exists (read-only SSH 2026-10-16).
- [open] Q1 OnFailure on the scheduler unit.
