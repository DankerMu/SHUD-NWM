# Design

## Decisions

- Why node-22 first: the scheduler must stop producing runs for the basin before node-27 retires it, and the
  node-27 tool can then prove from the manifest that the basin is out. Both runbook retirements did it in
  this order.
- Why wait instead of stopping the autopipe timer: a round in flight has read the old exclusion list and
  re-activates rows; only a round that started after the edit is known to honour it. Waiting needs no unit
  control on node-27 and has no "timer left stopped" failure state.
- Why supersede before deactivate: both need the exclusion first; with runs superseded and rows still
  active the basin shows an empty layer, with rows inactive and runs published the national river network
  drops it while its products are still candidates. Either order is transient; this one keeps the run
  backup before the step that writes the audit log.
- The supersede dry-run writes inside a transaction and rolls back; on a few hundred rows that is small, and
  it is the only check against the real database before an apply. A scratch-database run of the whole tool
  on node-27 is pending the RAID link repair; it must use a temporary env file (holding the scratch
  `DATABASE_URL`) and a stand-in `systemctl`, which the `DATABASE_URL` binding enforces.
- One basin version per run, several per succession: the node-22 removal may name several basins, and
  per-basin directories keep receipts and backups apart without a multi-basin transaction.
- The manifest precondition is per basin, not per basin version, because the exclusion list is per basin:
  excluding a basin that still has a live version would stop that version's ingest.
- One instance at a time (a lock beside the env file): the env edit is read-modify-rename, and two
  retirements waiting in parallel would lose one of the keys.

- The wait follows start timestamps instead of waiting for the unit to be at rest: on 2026-10-07 the
  production rounds ran about 13 minutes against a 10 minute timer, back to back for hours, so "at rest"
  was never observable and a wait for it would time out after the env file was already edited.

## Failure and recovery

| Failure | State left | Way on |
|---|---|---|
| `exclude` fails before the rename | env file unchanged; a backup may exist | rerun; an existing backup of this succession is kept and not rewritten |
| wait times out | key in the env file, no receipt | rerun waits again |
| `supersede` fails | transaction rolled back; backup CSV removed | rerun |
| process killed during `supersede` before the commit | no row changed; backup CSV may be left | rerun finds the backup and candidate rows and fails; the operator compares, moves the CSV aside and reruns |
| `supersede` commits, receipt write fails | runs superseded, backup CSV on disk, no receipt | rerun finds zero rows in the three statuses and an existing backup: it writes the receipt from the backup's row count without a second backup |
| `deactivate` fails part-way | some rows inactive; failure receipt names them | rerun acts on rows still active |
| `verify` fails | nothing written by it | fix the cause (the exclusion entry), rerun |

Nothing in the tool reverts a step. Reviving a basin is manual, from the env backup and the run CSV, and is
described in the runbook.
