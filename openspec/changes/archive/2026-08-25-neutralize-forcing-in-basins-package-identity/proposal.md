# Neutralize forcing CSVs in basins package identity

## Why

A basins package declares `forcing.policy = "excluded_by_default"` and
`payload_copied = false`, and in the same manifest folds `csv_count`,
`byte_count`, and `aggregate_checksum` — all derived by reading every forcing
CSV — into `content_sha256` and `package_checksum`. `copy_forcing` gates only
whether payloads are appended to `source_files`
(`workers/model_registry/basins_package.py:1194-1210`); it never gates the scan
or the hash. Production always publishes with `copy_forcing=False`
(`scripts/publish_scheduler_file_registry.py:297`), so the contradiction is the
permanent production state.

A second, independent leg carries the same dependency: discovery writes
`forcing_csv_count` into the inventory document
(`workers/model_registry/basins_discovery.py:291`), and the whole document is
hashed raw into `source_inventory_checksum`
(`workers/model_registry/basins_package.py:320`), which the cutover gate lists
as a nested identity field (`scripts/scheduler_file_provider_refresh.py:164`).

Consequences, in the order they bite:

1. **#1702 item 3 is jointly unsatisfiable as written.** Deleting an unused
   forcing CSV and recalibrating a basin's mesh are indistinguishable at the
   cutover gate — both land in `package_changed`, both demand a per-basin
   declared cutover. #1702's Evidence Floor forbids triggering any cutover.
2. **126 GB of publish I/O per baseline round, permanently.** Every baseline
   publish sha256-reads the basin's entire forcing tree to produce an
   `aggregate_checksum` whose only consumer is the identity material it should
   not be in.
3. **The churn amplifies downstream.** Verified this change (see design.md):
   basins package identity propagates into the direct-grid variant id that
   `hydro.hydro_run.model_id` carries, so each declared cutover is equivalent to
   a new model downstream.

## What Changes

- **Ruling**: forcing CSV content is NOT part of basins package identity when
  the package declares it excluded. The declaration becomes true.
- `_forcing_checksum_material` reduces to `{"policy", "payload_copied"}` for
  both policies. Manifest evidence fields are untouched.
- When `copy_forcing=False`, the per-file sha256 loop is skipped entirely;
  `csv_count`/`byte_count` survive on `stat` alone.
- `forcing_dir_original_name` leaves `source_material`.
- Discovery stops emitting `forcing_csv_count` (zero production readers).
- `BASINS_PACKAGE_SCHEMA_VERSION` and the inventory schema version are bumped so
  the one-time identity change is attributable to a declared packaging
  migration, and so historical manifests remain reconstructable.
- `services/production_closure/object_store_validation.py` — which reconstructs
  `package_checksum` from stored manifests with its own copy of the material —
  branches on the stored `schema_version`, plus a parity test binding the two
  implementations.
- **Cleanup semantics pinned**: #1702 item 3 empties `forcing/`; it does not
  remove the directory. `forcing_dir` and `forcing_dir_original_name` have real
  production readers and legitimately vary with directory presence.
- ADR 0006 records the ruling and its one-time named churn.

## Impact

- Affected specs: `basins-asset-discovery`, `shud-model-package-publication`, `basins-registry-import`
- Affected code: `workers/model_registry/basins_package.py`,
  `workers/model_registry/basins_discovery.py`,
  `services/production_closure/object_store_validation.py`,
  `workers/model_registry/basins_registry_import.py`
- Unblocks: #1702 item 3 (cleanup becomes a true no-op)
- Not touched: the cutover gate and its fail-safe `refused` semantics (#1080),
  `Basins/` directory contents, #1720's prospective≡previous defect
