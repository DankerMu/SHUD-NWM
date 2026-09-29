## ADDED Requirements

### Requirement: Same-day rename leftovers are deleted only through a pinned, backed-up, dry-run-first one-shot
The repository SHALL provide `scripts/ops/node27_2621_delete_rename_leftovers.sql` and a matching `_rollback.sql`, executed only through `scripts/ops/node27_oneshot_sql.py`, that delete exactly the owner-named rename leftovers (`basins_dnzh_{mdzh,mj,mnzh,qtj}`, `basins_xinan_{dulongjiang,lancangjiang,nujiang}`) and their registry rows in one transaction. The delete SHALL raise, rolling back every change, unless: each target exists, is not `evidence-only`, is absent from the supplied manifest basin set, and names an existing successor with an active model; the live non-chunk FK set equals the pinned inventory and no user trigger exists on the deleted tables; every id set and row count equals the pinned inventory, the model set contains no active model, and no hydro run, forcing version, interpolation weight, state snapshot, display-coverage row or pipeline job references the targets; and, after the target parent rows are locked `FOR UPDATE`, the `hydro.river_timeseries`, `met.forcing_station_timeseries` and `met.forcing_station_timeseries_legacy` hypertables hold zero rows referencing the target keys. It SHALL back up exactly the rows it deletes before deleting, SHALL run only the `river_segment` and `met_station` deletes with `session_replication_role = replica` and restore `origin` immediately after each, SHALL refuse rather than fall back when that setting is not permitted, and SHALL verify zero orphans afterwards. The dry run is the runner default; `--apply` commits. The delete SHALL also refuse unless the target stations reference exactly the pinned canonical grid snapshots, the one parent outside the deleted tables. The rollback SHALL restore the backed-up rows byte-for-byte, preserving identity key values, and SHALL refuse, before restoring any row, to restore over rows that still exist, when the outbound foreign keys of the restored tables differ from the pinned inventory, or when a parent row outside the restored tables that the backed-up rows reference no longer exists.

#### Scenario: dry run reports without changing data
- **WHEN** the delete runs without `--apply` on the pinned targets
- **THEN** every gate, count and per-step timing is reported and the database is unchanged afterwards

#### Scenario: business data or drift aborts before deleting
- **WHEN** any target gains a hydro run, forcing version, interpolation weight, active model or manifest entry, or the FK inventory or row counts drift from the pinned values
- **THEN** the script raises, nothing is deleted, and the failing check is named

#### Scenario: hypertable reference aborts
- **WHEN** a hypertable row references a target river segment or station key
- **THEN** the zero-reference gate raises before any delete

#### Scenario: apply then rollback round-trips byte-exact
- **WHEN** the delete is applied with a fresh copy directory and the rollback is then run from that directory
- **THEN** the restored rows, including identity keys, are byte-identical to the originals

#### Scenario: rollback refuses when a restored row's parent is gone
- **WHEN** the rollback runs after a canonical grid snapshot referenced by the backed-up stations was removed, or after the restored tables gained an unpinned foreign key
- **THEN** the rollback raises naming the missing parent or the foreign-key drift, and no row is restored
