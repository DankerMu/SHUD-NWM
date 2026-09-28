# Design: state-index-capacity-watch

## Wrapper `scripts/node22_state_index_capacity_watch.py`

- **Call.** It calls `scripts.scheduler_state_index_repair.repair_state_index` in-process. It uses the same parameters as the CLI, with `operation="prune-retention"`, `enforce=False` and every selector `None`. The roots, prefix and lag come from the environment, exactly as the CLI resolves them.
  - `enforce=False` is a literal: the wrapper has no `--enforce` flag at all.
  - Under `enforce=False`, `repair_state_index` never resolves the archive or repair-receipt roots (`scripts/scheduler_state_index_repair.py` ~164-165), and the dry-run takes no lock.
- **Inputs.**
  - The environment, loaded by the unit from `compute.scheduler-dbfree.env`. That file supplies `OBJECT_STORE_ROOT`, `OBJECT_STORE_PREFIX`, `NHMS_OBJECT_STORE_COPYBACK_ROOT` and `NHMS_SCHEDULER_CYCLE_LAG_HOURS`.
    - This was checked on node-22 on 2026-09-28 with a read-only `grep`: lines 13-15 and `NHMS_SCHEDULER_CYCLE_LAG_HOURS=16`. The example file has the same keys at :60/:87/:88/:189.
    - The live `OBJECT_STORE_ROOT` equals the one in `compute.scheduler-provider-refresh.env` (`/scratch/frd_muziyao/nhms-prod/object-store`).
    - This is a recorded deviation from the issue's three-source wiring. One `EnvironmentFile` is simpler and matches the sibling units.
  - The receipt root, from `NHMS_STATE_INDEX_CAPACITY_WATCH_RECEIPT_ROOT` or `--receipt-root`. There is no default.
    - The same semantics as the repair CLI's `_private_root` apply (`scripts/scheduler_state_index_repair.py` ~336). The root must already exist, be a real directory, be owned by the effective uid, and be private (no group or other bits). Otherwise the wrapper refuses with exit 2.
    - The operator creates it once with `install -d -m 0700`. The wrapper never creates the root.
- **Alert rules.** Evaluated per lane on the dry-run summary:
  - `checksum_valid is not True` → alert `checksum_invalid`;
  - `retention.capacity_before.warning is True` → alert `capacity_warning`;
  - `retention.capacity_after.warning is True` → alert `capacity_warning_unprunable`. Pruning would not clear the warning, so the runbook escalates this to #2541 instead of re-running enforce;
  - `retention.entry_count_before == 0` → alert `lane_empty`. The index file exists but holds no entries. A wrong root or an unmounted NFS is a CLI refusal instead (`root_unavailable` / `provider_destination_missing`, exit 2);
  - a lane or its retention block is missing → alert `lane_summary_missing`.
- **Writer race.** The dry-run reads without a lock. A scheduler write between the two preimage reads raises `provider_preimage_changed` (`packages/common/provider_atomic.py` ~132-146), which surfaces as a `RepairCliError`.
  - Only this reason is retried: at most 3 attempts in total, 5 s apart.
  - `attempts` is recorded in the receipt.
  - Every other refusal is not retried.
- **Exit codes.** The unit fails on any non-zero code.

  | Code | Meaning |
  |---|---|
  | 0 | Healthy, receipt written. |
  | 1 | At least one alert. The receipt is still written. |
  | 2 | CLI refusal (`RepairCliError`, e.g. `repair_cycle_lag_unset` or a missing root), receipt root unset or unsafe, or receipt write failed. |
  | 3 | `RepairIncompleteError`. This cannot occur on a dry-run and is mapped defensively. |
  | 4 | Unexpected exception. The wrapper makes a best-effort `status=refused` receipt carrying `error_type`, so an import or programming error never looks like an alert and never leaves yesterday's `latest.json` looking current. |

  The receipt JSON is printed to stdout, which reaches the journal, **before** the files are written. A file-write failure therefore never loses the verdict.

- **Receipt.** Bounded and owner-private:
  - `<root>/<UTC-ts>.json` plus `<root>/latest.json`, directory mode `0700`, file mode `0600`.
  - Written with the `safe_fs` atomic no-follow writer, the same writer the repair CLI uses.
  - Rotation keeps the newest 30 timestamped receipts.
  - Contents:
    - `schema_version`, `started_at`, `finished_at`, `status` (`healthy` / `alert` / `refused`), `exit_code`, `alerts[]`;
    - `attempts`;
    - per lane: `root`, `index`, `checksum_valid`, `action`, `untouched_reason`, `entry_count_before`, `removed_count`, `removed_state_ids_sha256`, `retention_days`, `cycle_lag_hours`, and the full `capacity_before` / `capacity_after` objects;
    - on refusal: `error` (reason and field).
  - **Never** included: `removed_state_ids`, `groups`, or raw index content.
  - The same JSON is printed to stdout, so it reaches the journal.

## Units

- **`infra/systemd/nhms-scheduler-state-index-capacity.service`:**
  - `Type=oneshot`, `WorkingDirectory=/scratch/frd_muziyao/NWM`;
  - `EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env`;
  - `Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`;
  - `Environment=NHMS_STATE_INDEX_CAPACITY_WATCH_RECEIPT_ROOT=/scratch/frd_muziyao/nhms-prod/workspace/state-index-repair/capacity-watch`;
  - `ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.node22_state_index_capacity_watch`.
    - Use the `-m` form, not a script path. `scripts/` has no `__init__.py` and is not in the editable-install mapping. With `-m`, the `WorkingDirectory` (the repo root) goes on `sys.path`, so `scripts.scheduler_state_index_repair` imports.
    - The precedent is `scripts/scheduler_file_provider_refresh_once.sh:62` and the runbook's own `-m scripts.scheduler_state_index_repair`.
  - `TimeoutStartSec=900`.

  The interpreter is the exact one required by the node-22 maintenance window; no `uv`.
- **`...capacity.timer`:** `OnCalendar=*-*-* 05:30:00 UTC`, `RandomizedDelaySec=15m`, `Persistent=true`, `Unit=` the service, `WantedBy=default.target`.
- **Registration.** Both files are registered in the node-22 entrypoint invariant guards (`tests/test_node22_entrypoint_invariant.py` and `..._python_scan.py` governed lists), and get their own exact-field pin in the style of the journal-retention pin.

## Stage 2 decision: keep enforce manual

The stage 1 alert fires at 0.70 utilization. From 0.70 to the node cap is about 90k JSON nodes, or about 4,400 entries at roughly 20.5 nodes per entry. Measured growth between the two prunes was 384 entries in about 20 h; the in-window rate implied by the first prune is about 275 per day. That leaves about **10-15 days** of lead time after the alert.

An unattended enforce would have to do two things:
- stop and restart `nhms-compute-scheduler.timer` from inside a unit, which the node-22 probes and timers are read-only by precedent (D4) not to do;
- bypass the #2548 human review gate.

It would also need automatic recovery of the scheduler timer after an exit-3 partial commit. Manual enforce takes about 5 minutes of scheduler pause (measured on 2026-09-28). With about 10 days of lead and a daily failing unit, the risk-cost trade does not justify automation.

If steady-state utilization inside the 21-day window reaches 0.70, `capacity_warning_unprunable` fires. That is a capacity-design problem for #2541, not a cadence problem, and automating enforce would not fix it.

Revisit when stage 1 receipts show a cadence that makes manual runs burdensome. The decision is recorded in runbook §8.12 and in an issue comment. No ADR: the decision is easy to reverse, which fails the ADR "hard to reverse" test.

## Must-preserve

- `prune-retention` CLI behavior and exit codes are unchanged.
- No new writer of the state index.
- The existing units are unchanged.
