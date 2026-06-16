## 1. Discovery and Validation

- [x] 1.1 Extend q_down discovery to `LEFT JOIN met.forcing_version` and return `forcing_package_uri`, `checksum`, and raw `lineage_json` without dropping runs that lack metadata; parse lineage manifest URI/checksum in Python from dict/string/null inputs.
- [x] 1.2 Add forcing package key normalization and allowlist validation for exact `forcing/<source>/<cycle>/<basin_version_id>/<model_id>` keys.
- [x] 1.3 Add forcing package source-tree validation for missing directories, regular-file source keys, symlink roots/children, missing `forcing_package.json`, checksum mismatch, and same-package manifest file presence.

## 2. Copyback Behavior

- [x] 2.1 Copy selected forcing packages to `NHMS_OBJECT_STORE_COPYBACK_ROOT` before q_down display artifacts or DB publish state are advanced.
- [x] 2.2 Deduplicate copyback by normalized forcing package key while preserving per-run error details for missing metadata.
- [x] 2.3 Preserve existing `runs/<run_id>` copyback, exact-root skip, source-tree validation, and error normalization.
- [x] 2.4 Update copyback lineage to distinguish `runs` and `forcing_packages` while preserving existing summary fields.

## 3. Tests and Evidence

- [x] 3.1 Add `tests/test_tile_publisher.py` happy-path coverage with `run-a` + `forcing-1`, source key `forcing/gfs/2026061400/basin-1/model-1/`, manifest bytes whose SHA-256 equals DB checksum, and expected identical bytes in shared object-store plus unchanged `runs/run-a` copyback.
- [x] 3.2 Add shared-package dedupe coverage with two q_down runs referencing one normalized forcing key; expected lineage has two run entries and one forcing package entry.
- [x] 3.3 Add missing metadata failures: no `met.forcing_version`, missing `forcing_package_uri`, missing checksum; expected details include `run_id`, `forcing_version_id`, and missing field, with no stable q_down manifest advance.
- [x] 3.4 Add integrity/path failures: missing manifest, checksum mismatch, lineage checksum mismatch, wrong prefix, traversal/absolute key, source symlink, and regular-file source key.
- [x] 3.5 Add no-stable-artifact regression for forcing copyback failure and assert `NHMS_PUBLISHED_ARTIFACT_ROOT` does not receive forcing packages.
- [x] 3.6 Run `uv run pytest -q tests/test_tile_publisher.py`.
- [x] 3.7 Run `uv run ruff check services/tile_publisher/publisher.py tests/test_tile_publisher.py`.
