## ADDED Requirements

### Requirement: q_down publish mirrors canonical precipitation products
After a successful q_down publish for `(source, cycle)`, the publisher on node-22 SHALL mirror `canonical/<source>/<cycle>/prcp_rate_or_amount/*.nc` and the referenced `canonical/<source>/grid/<grid_id>/grid.json` from `OBJECT_STORE_ROOT` to `NHMS_OBJECT_STORE_COPYBACK_ROOT` under the same keyspace using the existing temp-tree + rollback copy pattern. The mirror MUST be idempotent (a destination file with identical size is skipped) and MUST NOT fail the q_down publish when the source products are missing; the failure MUST be recorded in copyback lineage as `precip_mirror: failed` with the missing path.

#### Scenario: Successful mirror
- **WHEN** q_down publish succeeds for `gfs` cycle `2026090212` and the source root holds 56 `.nc` files plus `grid.json`
- **THEN** the copyback root contains the same 56 files and `grid.json` with identical bytes
- **AND** lineage records `precip_mirror: ok` with the file count

#### Scenario: Idempotent re-run
- **WHEN** the mirror runs again for a cycle already mirrored
- **THEN** no file is rewritten and lineage records `precip_mirror: skipped`

#### Scenario: Missing source does not block publish
- **WHEN** the source `prcp_rate_or_amount` directory is absent
- **THEN** q_down publish still completes
- **AND** lineage records `precip_mirror: failed` naming the absent path

### Requirement: One-shot backfill mirrors retained cycles without touching the environment
A script `scripts/canonical_precip_copyback_backfill.py` SHALL mirror every retained `canonical/<source>/<cycle>/prcp_rate_or_amount/` directory and each source's `grid.json` using only the standard library, runnable on node-22 as `/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill --source-root <root> --copyback-root <root>`; it MUST print a JSON summary (per cycle: copied/skipped/failed) and MUST NOT import project modules that require third-party packages.

#### Scenario: Backfill summary
- **WHEN** the script runs against a source root with two sources and N cycles each
- **THEN** it exits 0 and prints a JSON summary listing every cycle with `copied`, `skipped`, and `failed` counts

#### Scenario: Dry run
- **WHEN** `--dry-run` is passed
- **THEN** no file is written and the summary reports the planned copies

### Requirement: Canonical mirror is pruned with the raw retention watermark
`scripts/node27_raw_retention.py` SHALL include `canonical/<source>/<cycle>` directories in its retention targets using the same keep watermark applied to `raw/<source>/<cycle>`, and MUST never delete `canonical/<source>/grid/`.

#### Scenario: Old canonical cycle pruned
- **WHEN** a canonical cycle directory is older than the retention watermark
- **THEN** it is listed in the retention targets and removed in the same run as the corresponding raw cycle

#### Scenario: Grid definitions are preserved
- **WHEN** retention runs
- **THEN** `canonical/<source>/grid/**` is never a target regardless of age
