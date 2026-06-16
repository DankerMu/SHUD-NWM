## Implementation Tasks

- [x] Add the forcing copyback backfill command and configuration loading.
- [x] Implement DB discovery for q_down-capable historical runs in statuses `parsed`, `frequency_done`, and `published`.
- [x] Reuse or expose #493 forcing package validation/copy helpers without duplicating path/checksum rules.
- [x] Implement dry-run planning, explicit `--apply`, dedupe, already-present detection, and JSON report output.
- [x] Add focused tests for dry-run, apply, already-present, missing source, checksum mismatch, legacy key rejection, unsafe path rejection, and duplicate key behavior.
- [x] Add node-22 operator docs for command, env vars, rerun behavior, and rollback boundaries.

## Required Evidence

- [x] `uv run --no-sync pytest -q tests/test_forcing_copyback_backfill.py`
- [x] `uv run --no-sync pytest -q tests/test_tile_publisher.py tests/test_forcing_copyback_backfill.py`
- [x] DB discovery case: seed `parsed`, `frequency_done`, `published`, and excluded-status `hydro.hydro_run` rows; seed q_down and non-q_down `hydro.river_timeseries`; seed joined and missing `met.forcing_version` rows -> only eligible q_down runs are counted, excluded/non-q_down runs are omitted, and `forcing_version_count` reflects distinct joined forcing versions. SQLite schema tests are acceptable for this issue because the query uses portable joins/filters only and does not depend on Timescale hypertable behavior; production PostGIS/Timescale roundtrip remains a manual environment check outside CI.
- [x] CLI dry-run case: invoke `uv run python -m services.tile_publisher.forcing_copyback_backfill` with `DATABASE_URL`, `OBJECT_STORE_ROOT`, and `NHMS_OBJECT_STORE_COPYBACK_ROOT` -> exits 0, emits JSON, and writes nothing.
- [x] CLI apply case: invoke the same module with `--apply` -> writes validated missing package and reports copied count.
- [x] CLI config failure case: missing `DATABASE_URL`, `OBJECT_STORE_ROOT`, or `NHMS_OBJECT_STORE_COPYBACK_ROOT` -> stable non-zero exit/error JSON or stderr message, no target writes.
- [x] Dry-run case: seeded valid q_down run with missing target -> report `copyable_package_count=1`, no target files created.
- [x] Apply case: same seed with `--apply` -> target `forcing/.../forcing_package.json` exists and copied count is 1.
- [x] Already-present case: target manifest checksum matches DB checksum -> counted as already present, not copied.
- [x] Failure case: missing source package -> `missing_source_count=1` and failure row includes `run_id`, `forcing_version_id`, `forcing_package_uri`, `reason`.
- [x] Failure case: source manifest checksum mismatch -> `checksum_mismatch_count=1`, no copied success.
- [x] Failure case: `forcing/{forcing_version_id}/` legacy key -> `legacy_key_rejected_count=1`, no guessed migration.
- [x] Failure case: traversal/absolute/wrong prefix/symlink/non-directory source -> failure/manual row, no target write.
- [x] Failure case: forcing URI source/cycle/basin/model differs from `hydro.hydro_run` identity -> stable failure/manual row, no target write.
- [x] Failure case: lineage `forcing_package_manifest_checksum` differs from `met.forcing_version.checksum` -> stable failure/manual row, no target write.
- [x] Target safety case: apply with existing target as regular file, symlink, or otherwise unsafe tree -> failure row with reason, `copied_count=0`, no partial package.
- [x] Target preservation case: apply failure after a valid target exists -> previous valid target remains readable and unchanged.
- [x] Dedupe case: multiple runs sharing normalized forcing key -> one package plan/action with all related identities retained.

## Documentation Evidence

- [x] Node-22 command includes `uv run python -m services.tile_publisher.forcing_copyback_backfill` and explicit `--apply` for writes.
- [x] Env var section names `DATABASE_URL`, `OBJECT_STORE_ROOT`, `OBJECT_STORE_PREFIX` if needed, and `NHMS_OBJECT_STORE_COPYBACK_ROOT`.
- [x] Rerun section states dry-run/apply are idempotent for checksum-consistent targets.
- [x] Rollback section states the tool does not mutate DB rows; package rollback is manual removal/restoration under the shared object-store for packages reported as copied.

## Non-Goals / Out of Scope

- [x] No production command execution in CI.
- [x] No automatic migration for legacy forcing package keys.
- [x] No DB writes or status transitions.
- [x] No frontend changes.
