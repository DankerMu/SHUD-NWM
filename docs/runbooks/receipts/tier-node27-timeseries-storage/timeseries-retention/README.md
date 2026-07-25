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

### `dry-run-first-live-20260725T054745Z.json`

First gates-passing dry-run (2026-07-25, Step B of issue #1071). Both
gate receipts were live and fresh: the recurring audit's completeness
receipt (generated `2026-07-25T03:40Z`, age ~2h < 26h bound) and the
drill's first live PASS receipt
(`../archive-rebuild-drill/first-live-pass-20260725T053420Z.json`).

Configuration: `WINDOW_DAYS=21` (cutoff `2026-06-24T12Z` against
watermark reference_time `2026-07-15T12Z`), `PER_TICK_BOUND=5`,
`ENFORCE=0`. **Why 21 and not the spec's planned 14**: a 14-day probe
(cutoff `2026-07-01T12Z`) was correctly refused with
`COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW` — 39 runs-lane subjects
in cycles from `2026-06-20T12Z` through `2026-06-25` were still pending-archive (mover
frontier at 2026-06-20), so the wider window keeps the drop window
`[2026-06-04, 2026-06-18]` clear of the pending frontier. That refusal
is the gate working as specified, not a bypass.

Result: `outcome=dry-run`, rc=0. Four candidate chunks
(`_hyper_3_8`, `_hyper_3_1`, `_hyper_1_6`, `_hyper_1_3` — both detail
hypertables represented) and two boundary-partial chunks correctly held
in `deferred_remainder` (`_hyper_3_7`, `_hyper_1_5`). No chunk was
dropped (dry-run).

## Enforce path status

All upstream gates are now live on node-27: the recurring
storage-inventory-audit produces fresh completeness receipts, the
compression timer runs (receipts under `../timeseries-compression/`),
and the drill PASS receipt above unlocks §6.3. The remaining §6.3 step
is the **first enforce run** (`drop_chunks` on production), which is
Step C of issue #1072 and requires explicit human authorization — it
must NOT be run as a follow-on of the dry-run.

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
