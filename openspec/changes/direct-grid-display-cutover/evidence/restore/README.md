# restore/ — provisioning ↔ cleanup mapping

`rehearse.py` runs the entire cleanup chain in its Restore section (both on
the successful path AND from the `except` handler on any mid-run failure).
The mapping below documents which cleanup step reverses which provisioning
step, plus the SQL/lifecycle op invoked.

## Reverse mapping table

| Provisioning step                                    | Cleanup counterpart                                                                                                                                                                                                                                                                       | Runs in                       |
|------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------|
| `provisioning/00-baseline-and-stations.sql`          | 1. `UPDATE core.model_instance SET active_flag=false, lifecycle_state='inactive' WHERE model_id='model__evidence_cmfd_p02_synth__v1'` <br> 2. `UPDATE met.met_station SET active_flag=false WHERE basin_version_id='basin__evidence_cmfd_p02_synth__v1'`                                    | `rehearse.py::_restore_synthetic_state` steps 2 + 3 |
| `provisioning/01-canonical-grid-snapshot.sql`        | (Deliberately RETAINED across restore.) The canonical grid snapshot row is an append-only registry entry; deleting it would cascade-delete `met.canonical_grid_cell` rows that could be referenced elsewhere. The row's `applicable_source_ids=['cmfd']` marker keeps it as evidence-only. | not restored (evidence)       |
| `provisioning/02-register-direct-grid-variant.py`    | 1. Change 4 `deactivate` lifecycle op (`trusted_internal=True`, `override_missing_active=True`, `reason='Epic #992 SUB-7 rehearsal restore'`) on the M1 target model_id <br> 2. The M1 mirror rows are UPDATE'd `active_flag=false` alongside the synthetic-basin blanket UPDATE in step 1 above. The M1 `core.model_instance` row and its 3 mirror rows in `met.met_station` are RETAINED post-restore as evidence-only inactive rows (matching the pattern used by `register-synth-p02.sql`). | `rehearse.py::_restore_synthetic_state` step 1 |
| `provisioning/03-seeded-forecast-run.sql`            | `DELETE FROM hydro.hydro_run WHERE run_id='run__evidence_cmfd_p02_synth__rehearsal_pre_cutover_v1'`                                                                                                                                                                                                                                                | `rehearse.py::_restore_synthetic_state` step 4 |

## Post-restore invariants

`rehearse.py` runs `_production_baseline_assert` after restore and writes
`rehearse/production-scoped-assertions.after-restore.log`. The assertions
prove:

1. `core.basin_version` state unchanged (the `basin__evidence_cmfd_p02_synth__v1`
   row was pre-existing in the readiness archive, not created here).
2. `core.model_instance`:
   - `model__evidence_cmfd_p02_synth__v1` back to `active_flag=false, lifecycle_state='inactive'`.
   - M1 target (`dg_...`) `active_flag=false, lifecycle_state='inactive'` (via Change 4 deactivate).
   - 13 production active model_instance rows unchanged.
3. `met.met_station`:
   - 3 legacy `synth-station-001..003` rows back to `active_flag=false`.
   - 3 M1 mirror rows (`synth-mip-m1-v2::cell:...`) `active_flag=false`.
   - 6290 production active_flag=true rows across 13 basin_version_ids unchanged.
4. `hydro.hydro_run`:
   - Seeded pre-cutover run deleted.
   - No new `model__evidence%` hydro_run row created during the window.
5. `met.canonical_grid_snapshot`: rehearsal grid snapshot retained
   (see the note in the table above; append-only registry).
6. `ops.audit_log`: append-only. Every hook skip / approval / lifecycle
   audit row committed during the window is retained by design — the
   Change 4 `ops.audit_log` write of `state_clone_cold_start_approved` is
   permanent evidence of the approval obligation.

## Emergency manual restore

If `rehearse.py` cannot run (e.g., Python env broken mid-Phase B), execute
these SQL statements directly against the DB to restore the pre-rehearsal
state. All are idempotent.

```sql
BEGIN;
UPDATE core.model_instance
   SET active_flag = false, lifecycle_state = 'inactive'
 WHERE model_id = 'model__evidence_cmfd_p02_synth__v1';
-- Also deactivate the M1 target if it was activated. Replace <M1_MODEL_ID>
-- with the model_id printed by 02-register-direct-grid-variant.py.
UPDATE core.model_instance
   SET active_flag = false, lifecycle_state = 'inactive'
 WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1'
   AND resource_profile->'direct_grid_forcing'->>'model_input_package_id' = 'synth-mip-m1-v2';
UPDATE met.met_station
   SET active_flag = false
 WHERE basin_version_id = 'basin__evidence_cmfd_p02_synth__v1';
DELETE FROM hydro.hydro_run
 WHERE run_id = 'run__evidence_cmfd_p02_synth__rehearsal_pre_cutover_v1';
COMMIT;
```

Note the manual path does NOT go through the Change 4 `deactivate` lifecycle
op, so the lifecycle audit row for the deactivation is NOT emitted. This is
acceptable ONLY as an emergency path — the normal `rehearse.py` path
records the audit row.
