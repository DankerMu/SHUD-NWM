# Retention live receipts (task §6.3)

This directory holds committed live receipts from `scripts/node27_timeseries_retention.py` on node-27's primary Postgres (`127.0.0.1:55432`).

## Receipts

### `refusal-completeness-missing-20260713T030936Z.json`

First live invocation of the retention runner on node-27, executed
under the `nwm` user against the `nhms` database in container
`nhms-db`. The env pointed the completeness receipt at a path that
does not exist because the upstream storage-inventory-audit runner
(task §2.3, issue #849) has not landed its first live completeness
receipt on node-27 yet — the audit systemd timer is not enabled.

Result: `outcome=refused`, `refusal_reason=COMPLETENESS_RECEIPT_MISSING`,
exit code 1, `mode=enforce`. Schema-valid per
`schemas/timeseries_retention_receipt.schema.json` (`oneOf` refused
branch). This satisfies §6.3 test row 1 ("Missing or stale completeness
receipt … Expected: refusal, non-zero exit, reason in the receipt").

## Enforce path status

The dry-run + first-enforce evidence required by §6.3's remaining
three test rows depends on upstream live receipts that have not yet
landed on node-27:

- `nhms-node27-storage-inventory-audit.{service,timer}` — the systemd
  units defined by issue #849 are NOT installed on node-27; no
  archive-completeness receipt exists to feed the retention gate.
- `nhms-node27-timeseries-compression.{service,timer}` — the systemd
  units from issue #853 are NOT installed on node-27; compression
  migration 000047 has not been applied against the live TimescaleDB;
  there are no compressed chunks to observe under retention.
- `scripts/node27_archive_rebuild_drill.py` §5.2 live PASS receipt
  (issue #854 follow-up) — has NOT been produced against a fresh
  staging DB on node-27.

Once these upstream live receipts land under their respective issues,
the retention dry-run + enforce sequence in §6.3 can be exercised end
to end. Until then, this refusal-path receipt is the only live
retention evidence achievable without touching production data
without a valid gate.

## Reproduction

Env file at `/home/nwm/NWM/infra/env/node27-timeseries-retention.env`
(mode 0600). Invocation from node-27, worktree at
`/home/nwm/NWM-tier`:

```bash
set -a && . /home/nwm/NWM/infra/env/node27-timeseries-retention.env && set +a
export NODE27_TIMESERIES_RETENTION_RECEIPT_PATH="/home/nwm/node27-timeseries-retention-logs/$(basename ...).json"
cd /home/nwm/NWM-tier
/home/nwm/.local/bin/uv run --frozen python scripts/node27_timeseries_retention.py --dry-run
# rc=1, refusal_reason=COMPLETENESS_RECEIPT_MISSING
```

Runner invocation used `--dry-run` CLI flag; refused receipts always
carry `mode=enforce` per schema `oneOf` pin (documented in runbook
§8.5 and design.md #855 fixture block; behavior-lock test at
`tests/test_node27_timeseries_retention.py::test_dry_run_evaluates_gates_before_dryrun_branch`).
