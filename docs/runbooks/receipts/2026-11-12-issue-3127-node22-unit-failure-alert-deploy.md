# Receipt: node-22 unit-failure alert lane deployment (issue 3127)

- Date: 2026-11-12 (UTC). Operator: Claude Code session on the owner's behalf. Owner decision: D1-A (SMTPS shim through
  `scripts/node22_smtp_sendmail.sh`, PR 3128), D2 (reuse node-27 frontier-alert recipient/sender), D3 (template plus
  the six wired oneshot units).
- Host: node-22 login host, user systemd manager, checkout `/scratch/frd_muziyao/NWM` at `4e2304b47` (ff-only, clean
  tree before and after).
- Trigger: the first `nhms-scheduler-state-index-prune.service` failure (issue 3121) logged
  `Failed to enqueue OnFailure= job, ignoring: Unit nhms-node22-unit-failure-alert@nhms-scheduler-state-index-prune.service.service not found.`

## Before

`.venv/bin/python -m scripts.node22_unit_drift_check` → rc=1 `drift`:

- `missing`: `nhms-node22-unit-failure-alert@.service`
- `differs` (only non-comment delta: the missing `OnFailure=nhms-node22-unit-failure-alert@%n.service` line):
  refresh-timer-health, scheduler-stall-health, scheduler-evidence-retention, scheduler-file-provider-refresh,
  scheduler-journal-retention, scheduler-state-index-capacity
- `untracked`: `nhms-slurm-gateway.service.d/20-runtime-path.conf` (out of scope, left as is)
- `infra/env/node22-unit-failure-alert.env`: absent.

## Steps

1. Previous copies of the six units backed up to
   `/scratch/frd_muziyao/nhms-prod/workspace/repairs/unit-failure-alert/backup-20261112/` (0700, outside the systemd
   directory).
2. `install -m 0600` of the tracked template and six units into `~/.config/systemd/user/`; `systemctl --user
   daemon-reload`. No running service was restarted; `nhms-compute-scheduler.service` was not touched.
3. `infra/env/node22-unit-failure-alert.env` written 0600 (gitignored; `git status --porcelain` empty afterwards):
   `NHMS_FRONTIER_SENDMAIL=/scratch/frd_muziyao/NWM/scripts/node22_smtp_sendmail.sh`; `NHMS_ALERT_EMAIL_TO/FROM` and
   `NHMS_SMTP_HOST/PORT/USER/PASS` piped host-to-host from node-27's frontier-alert env without being displayed. No
   credential was rotated.
4. `systemd-analyze --user verify` on the template and on an instance: no output, rc=0.
5. Before PR 3128 merged, a dry instance logged `SKIPPED reason=SENDMAIL_UNAVAILABLE` (expected: wrapper absent).
6. After the merge and ff-pull (wrapper mode `-rwxr-xr-x`):
   - `systemctl --user start nhms-node22-unit-failure-alert@nhms-deploy-check.service.service` →
     `SMTP-ACCEPTED host=<masked> code=250 recipients=1`, `SENT unit=nhms-deploy-check.service`.
   - Real `OnFailure=` chain: `systemd-run --user --unit=nhms-deploy-check-fail -p
     "OnFailure=nhms-node22-unit-failure-alert@nhms-deploy-check-fail.service.service" /bin/false` →
     `Triggering OnFailure= dependencies.` → `SMTP-ACCEPTED ... code=250` → `SENT unit=nhms-deploy-check-fail.service`;
     the transient unit was `reset-failed` afterwards.

## After

- Drift probe (2026-11-12T14:29:44Z): all 22 manifest entries `same` (manifest sha256 prefix `935bafa09eaa`); rc=1 only
  because of the out-of-scope untracked drop-in above.
- `systemctl --user show -p OnFailure` on the seven wired units (the six above plus
  `nhms-scheduler-state-index-prune.service`) each names `nhms-node22-unit-failure-alert@<unit>.service`.

## Rollback

Copy the six files back from the backup directory, remove `~/.config/systemd/user/nhms-node22-unit-failure-alert@.service`
and the env file, `systemctl --user daemon-reload`.
