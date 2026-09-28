# Proposal: state-index-capacity-watch (#2653)

## Why

PR #2652 (#2548) shipped `prune-retention` as an operator-run command only. Nothing runs it on a schedule. The only capacity signal is `capacity.warning`: an evidence field plus one `logger.warning`. Both land in a journal that nobody reads.

node-22 has no `OnFailure=` mail channel. A failed user unit is its only alerting surface. As a result, after a prune, the index can drift back to the 300k-node / 16 MiB cliff unnoticed. Once it gets there, `state_save_qc`, `set_usable_flag`, copyback and warm-start reads all fail closed (#2548).

Live data from node-22:
- **First prune**, 2026-09-27: both indexes went from 9950 to 5772 entries.
- **Second prune**, 2026-09-28: both went from 6156 to 5884 entries, with utilization at 0.368. The index grew by 384 entries in about 20 h in between.

## What changes

**Stage 1: implemented.**
- A daily node-22 user-level timer with a oneshot service. It runs the `prune-retention` **dry-run only**, through a new wrapper script.
- The wrapper writes a bounded receipt for each run.
- The wrapper exits non-zero, so the unit fails, when any of these holds:
  - either lane has `capacity.warning=true`;
  - either lane has `checksum_valid=false`;
  - either lane would still have `capacity.warning=true` after pruning (`capacity_warning_unprunable`);
  - either lane has `entry_count_before == 0`: the index exists but holds no entries;
  - the CLI refuses, for example `repair_cycle_lag_unset`, or reports an incomplete result;
  - the watch itself fails unexpectedly.
- The wrapper never passes `enforce`. It never writes the index, the archive root, or the repair receipt root.

**Stage 2: decision.** Enforce stays manual, driven by the stage 1 alert. It is not automated in this change. The rationale is in `design.md`; the decision is recorded in the runbook and on the issue.

**Runbook.** §8.12 moves from "manual cadence" to the timer model:
- install and enable the units;
- read the failed unit and its receipt;
- on an alert, run the existing manual dry-run → review → enforce flow.

## Deviations from the issue

- **Environment.** The env comes from the single `compute.scheduler-dbfree.env`, not from the three sources the issue lists. That file carries all four keys; this was verified live on node-22 (see design.md).

## Fixture level

`compact`. The change is a read-only wrapper plus two unit files and runbook text. It adds no new state-mutation path.

## Out of scope

- Deleting state objects, or pruning inside the publisher.
- Sharding or batch upsert.
- Any change to the K1-K5 retention semantics.
- An automated enforce, per the stage 2 decision.
- Grading capacity in `node22_scheduler_stall_health.py`, which the issue explicitly rejects.
- Deploying other master changes to node-22. Pulling the node-22 checkout to install these units is a separate operator step, confirmed before it is done.
