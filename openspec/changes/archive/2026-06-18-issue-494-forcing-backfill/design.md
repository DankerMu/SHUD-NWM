## Change Surface

- New operator entrypoint: `uv run python -m services.tile_publisher.forcing_copyback_backfill`.
- Reused validation/copyback surfaces from `services/tile_publisher/publisher.py`: forcing package key normalization, identity/checksum validation, bounded tree validation, safe target replacement, rollback helpers.
- DB discovery over `hydro.hydro_run`, q_down `hydro.river_timeseries`, and `met.forcing_version`.
- Tests focused in `tests/test_forcing_copyback_backfill.py` plus unchanged #493 publisher tests.
- Focused docs for node-22 command/env/rerun/rollback.

## Must Preserve

- #493 q_down publish-time copyback remains the source of truth for validation rules.
- `NHMS_PUBLISHED_ARTIFACT_ROOT` remains display-only; backfill targets `NHMS_OBJECT_STORE_COPYBACK_ROOT`.
- Existing run product copyback and q_down/flood publication tests remain compatible.
- Dry-run is the default and performs no writes, directory creation, cleanup, or target replacement.

## Must Add / Change

- A dry-run/apply command that reads required env/config, scans historical q_down runs, dedupes by normalized forcing package key, and emits JSON by default.
- `--apply` is required for writes. Without `--apply`, the command only validates and reports.
- The report includes total run count, forcing version count, copyable package count, already-present checksum-consistent count, missing source count, checksum mismatch count, legacy key rejected count, and per-failure entries with `run_id`, `forcing_version_id`, `forcing_package_uri`, and `reason`.
- Existing target packages with matching `forcing_package.json` checksum are reported as already present and not counted as copied.
- Missing source, checksum mismatch, legacy key, path traversal, wrong prefix/shape, symlink/non-directory source, and target validation failures are reported without being marked success.

## Risk Packs Considered

- Public API / CLI / script entry: selected - new operator command and flags.
- Config / project setup: selected - requires `DATABASE_URL`, `OBJECT_STORE_ROOT`, and `NHMS_OBJECT_STORE_COPYBACK_ROOT`.
- File IO / path safety / overwrite: selected - scans source object-store trees and writes shared object-store packages in apply mode.
- Schema / columns / units / field names: selected - depends on `hydro.hydro_run`, q_down timeseries existence, and `met.forcing_version` fields.
- Auth / permissions / secrets: not selected - no new credential handling beyond existing env-provided DB URL.
- Concurrency / shared state / ordering: selected - command must be rerunnable and idempotent against already-present packages.
- Resource limits / large input / discovery: selected - discovery and copy use scoped package keys and #493 bounded tree helpers.
- Legacy compatibility / examples: selected - legacy `forcing/{forcing_version_id}/` keys are rejected for manual handling, not guessed.
- Error handling / rollback / partial outputs: selected - apply failures must produce stable failure rows and must not mark partial packages copied.
- Release / packaging / dependency compatibility: not selected - no new third-party dependency is expected.
- Documentation / migration notes: selected - node-22 execution and rerun/rollback instructions are acceptance criteria.
- Geospatial / CRS / basin geometry: not selected - package bytes are copied, not interpreted.
- Hydro-met time series / forcing windows: selected - forcing package identity includes source/cycle/basin/model, but scientific values are not changed.
- SHUD numerical runtime / conservation / NaN: not selected - no model execution.
- PostGIS / TimescaleDB domain behavior: selected - discovery must be valid against PostgreSQL/Timescale-backed production tables.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm operation.
- External hydro-met providers / snapshot reproducibility: not selected - no provider fetch or snapshot mutation.
- Run manifest / QC provenance: selected - manifest checksum binds DB forcing row to copied bytes.
- Published NHMS artifacts / display identity: selected - node-27 display/runtime identity requires shared forcing package keys to match source keys.

## Invariant Matrix

Governing invariant: The backfill command may copy a historical forcing package only when the same #493 normalized key and manifest checksum prove that the source package and target package identity are safe and exact.

Source-of-truth identity/contract: `hydro.hydro_run.{run_id,status,forcing_version_id,source_id,cycle_time,basin_version_id,model_id}` joined to `met.forcing_version.{forcing_version_id,forcing_package_uri,checksum,lineage_json}` and normalized to `forcing/<source>/<cycle>/<basin_version_id>/<model_id>`.

Surfaces:

- Producers: `workers/forcing_producer/producer.py` existing forcing package writer, unchanged.
- Validators/preflight: #493 forcing key/checksum/source-tree helpers in `services/tile_publisher/publisher.py`, reused rather than duplicated.
- Storage/cache/query: production DB query plus `LocalObjectStore` rooted at `OBJECT_STORE_ROOT` and `NHMS_OBJECT_STORE_COPYBACK_ROOT`.
- Public routes/entrypoints: new module CLI only; no API route.
- Frontend/downstream consumers: node-27 readonly object-store consumers that need `forcing/...` under the shared root.
- Failure paths/rollback/stale state: dry-run no-op writes, apply per-package copy/skip/failure, target rollback from #493 helper behavior.
- Evidence/audit/readiness: JSON report and optional operator-saved stdout/stderr.

Regression rows:

- Valid parsed/frequency_done/published q_down run with matching manifest checksum and missing target -> dry-run reports copyable; apply copies `forcing/...` under shared object-store and counts copied.
- Two runs or forcing versions resolving to the same normalized key -> one package plan/action, with all related run/forcing identities retained in report evidence.
- Target already has matching manifest checksum -> report `already_present`; apply does not count it as copied.
- Legacy `forcing/{forcing_version_id}/`, traversal, absolute path, wrong prefix, wrong segment count, empty segment, symlink source, regular-file source, missing manifest, missing source, or checksum mismatch -> report failure/manual item with `run_id`, `forcing_version_id`, `forcing_package_uri`, and reason; do not copy.
- `--apply` omitted -> no writes even for copyable packages.
- Existing sibling consumer -> #493 publish-time copyback tests continue to pass.

## Boundary-Surface Checklist

- Shared helper roots: #493 copyback validation/copy helpers; extract public/private helper boundaries only as needed for reuse.
- Public entrypoints: `services.tile_publisher.forcing_copyback_backfill` CLI.
- Read surfaces: production DB rows and source object-store package trees.
- Write/delete/overwrite surfaces: `NHMS_OBJECT_STORE_COPYBACK_ROOT` only in `--apply`; no DB writes.
- Staging/publish/rollback surfaces: #493 safe target replacement and rollback behavior.
- Producer/consumer evidence boundaries: manifest checksum and report rows.
- Stale-state/idempotency boundaries: already-present target, repeated dry-run/apply, duplicate forcing keys.
- Unchanged downstream consumers: node-27 display/runtime read paths and existing publisher behavior.

## Review Focus

- The command cannot write without explicit `--apply`.
- Backfill uses #493 validators instead of a second path/checksum rule set.
- Counts and failure rows are deterministic and auditable.
- Legacy keys are rejected for manual handling rather than guessed.
- Apply mode is idempotent for already-present checksum-consistent targets and has stable partial-failure reporting.
