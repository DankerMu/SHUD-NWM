# provisioning/ — task 4.1 (recorded bypass)

Provisions the rehearsal preconditions on the synthetic evidence identity
(`basin__evidence_cmfd_p02_synth__v1`) so that Phase B's real Change 4
`activate` cutover has:

- a synthetic previous-active baseline `core.model_instance` to supersede;
- a Change-4-shaped M1 target the flip hook can re-point onto;
- a legacy-shaped display set (`synth-station-001..003`) as the "before" set;
- a pre-cutover forecast run so the basin surfaces in `has_display_product`
  discovery, making the popup path reachable.

Every provisioning step has a matching cleanup counterpart under `../restore/`
(see `restore/README.md` for the pairing).

## Execution order (Phase B)

Run in this exact order — later steps read state written by earlier ones:

| # | Step                                    | Command (node-27)                                                                                                              | Type          |
|---|-----------------------------------------|--------------------------------------------------------------------------------------------------------------------------------|---------------|
| 0 | Baseline flip + synthetic stations on   | `docker exec -i nhms-db psql -U nhms -d nhms -v ON_ERROR_STOP=1 -f 00-baseline-and-stations.sql`                                | recorded SQL bypass |
| 1 | Insert canonical grid snapshot          | `docker exec -i nhms-db psql -U nhms -d nhms -v ON_ERROR_STOP=1 -f 01-canonical-grid-snapshot.sql`                              | recorded SQL bypass |
| 2 | Register M1 target via Change 4 surface | `DATABASE_URL="postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms" uv run python 02-register-direct-grid-variant.py`                | real registration surface |
| 3 | Seed pre-cutover forecast run           | `docker exec -i nhms-db psql -U nhms -d nhms -v ON_ERROR_STOP=1 -f 03-seeded-forecast-run.sql`                                  | recorded SQL bypass |

Step 2 is the ONLY step that exercises production code
(`workers.model_registry.direct_grid_variant_registration.register_direct_grid_variant`);
steps 0/1/3 are recorded evidence-only bypasses whose purpose is to
short-circuit setup so the rehearsed lifecycle op (Change 4 `activate`) is
the real path.

## Cleanup mapping (see `../restore/README.md` for full detail)

| Provisioning step | Cleanup step                                                                                       |
|-------------------|-----------------------------------------------------------------------------------------------------|
| 00-baseline-and-stations.sql | `UPDATE core.model_instance SET active_flag=false, lifecycle_state='inactive' WHERE model_id='model__evidence_cmfd_p02_synth__v1'; UPDATE met.met_station SET active_flag=false WHERE basin_version_id='basin__evidence_cmfd_p02_synth__v1'` |
| 01-canonical-grid-snapshot.sql | `DELETE FROM met.canonical_grid_snapshot WHERE grid_id='synth-grid-p0.2-m1-v2'` (cascades to canonical_grid_cell) |
| 02-register-direct-grid-variant.py | `DELETE FROM met.met_station WHERE basin_version_id='basin__evidence_cmfd_p02_synth__v1' AND station_role='direct_grid_cache' AND properties_json->>'model_input_package_id'='synth-mip-m1-v2'; DELETE FROM core.model_instance WHERE model_id=<M1 target model_id>` |
| 03-seeded-forecast-run.sql | `DELETE FROM hydro.hydro_run WHERE run_id='run__evidence_cmfd_p02_synth__rehearsal_pre_cutover_v1'` |

The rehearse.py script runs the full cleanup chain in its `except`/finally
handler on ANY mid-run failure and unconditionally in its post-rehearsal
Restore section.

## Synthetic package

The `synthetic-package/` subdirectory holds the M1 model asset package —
a minimal .mesh/.para/.calib set plus a §7.2-conformant binding manifest.
It is bit-stable and reproduced by hand; the checksum in
`package.manifest.sha256` is the reread evidence for Change 4's activation
preflight package-checksum verification (see `synthetic-package/README.md`).
The URI in `model_package_uri` points at the GitHub tree so preflight's
`_object_uri_prefix_status` classifies the scheme as `https` (supported).
