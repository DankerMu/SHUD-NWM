## Why

q_down publication currently mirrors `runs/<run_id>` to the shared object-store but leaves the run's `forcing/...` package only in the compute-node object-store. node-27 can therefore display published tiles and logs while lacking the forcing package needed for readonly inspection and reproducibility.

## What Changes

- Publish q_down MUST copy each successfully published run's referenced forcing package into `NHMS_OBJECT_STORE_COPYBACK_ROOT` under the same `forcing/<source>/<cycle>/<basin_version_id>/<model_id>` keyspace.
- The publisher MUST validate forcing metadata, key shape, source tree safety, `forcing_package.json`, and manifest checksum before marking q_down publication successful.
- q_down discovery MUST retain runs even when forcing metadata is missing, so copyback validation fails loudly instead of silently dropping runs.
- `object_store_copyback` lineage MUST distinguish run product copyback from forcing package copyback.

## Capabilities

### New Capabilities

- `qdown-forcing-copyback`: q_down publication mirrors referenced forcing packages to the shared object-store with strict key and checksum validation.

### Modified Capabilities

- None.

## Impact

- `services/tile_publisher/publisher.py`: q_down discovery, copyback planning, forcing package validation, lineage summary.
- `tests/test_tile_publisher.py`: happy path, dedupe, missing metadata, checksum mismatch, unsafe keys, symlink/tree safety, and no-published-forcing assertions.
- Future dependency: #494 can reuse the same forcing key and checksum validation for historical backfill.
