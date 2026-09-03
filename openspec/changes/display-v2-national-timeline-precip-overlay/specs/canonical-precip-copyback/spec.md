## ADDED Requirements

### Requirement: q_down publish mirrors canonical precipitation products
After a successful q_down publish for `(source, cycle)`, the publisher on node-22 SHALL mirror `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/*.nc` and the referenced `canonical/<storage_source>/grid/<grid_id>/grid.json` (storage source `gfs`/`IFS` via `normalize_source_id`, cycle token `%Y%m%d%H`) from `OBJECT_STORE_ROOT` to `NHMS_OBJECT_STORE_COPYBACK_ROOT` under the same keyspace using the existing temp-tree + rollback copy pattern. The mirror MUST be idempotent (a destination file with identical size is skipped) and MUST NOT fail the q_down publish when the source products are missing; the failure MUST be recorded in copyback lineage as `precip_mirror: failed` with the missing path.

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
A script `scripts/canonical_precip_copyback_backfill.py` SHALL mirror every retained `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/` directory and each source's `grid.json` using only the standard library, runnable on node-22 as `/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill --source-root <root> --copyback-root <root>`; it MUST print a JSON summary (per cycle: copied/skipped/failed) and MUST NOT import project modules that require third-party packages.

#### Scenario: Backfill summary
- **WHEN** the script runs against a source root with two sources and N cycles each
- **THEN** it exits 0 and prints a JSON summary listing every cycle with `copied`, `skipped`, and `failed` counts

#### Scenario: Dry run
- **WHEN** `--dry-run` is passed
- **THEN** no file is written and the summary reports the planned copies

### Requirement: Canonical mirror is pruned with the raw retention watermark
`scripts/node27_raw_retention.py` SHALL include `canonical/<storage_source>/<cycle_token>` directories in its retention targets using the same cutoff it applies to `raw/<source>/<cycle_token>` (`display_watermark − retention_days`, anchor unchanged), and MUST never delete `canonical/<storage_source>/grid/`. Because the script's configured sources are lower-case (`gfs`, `ifs`) while canonical directories carry the storage spelling (`gfs`, `IFS`), the canonical target path MUST be derived with `packages/common/source_identity.py::normalize_source_id`, not by reusing the raw source token verbatim.

#### Scenario: Old canonical cycle pruned
- **WHEN** a canonical cycle directory is older than the retention cutoff
- **THEN** it is listed in the retention targets and removed in the same run as the corresponding raw cycle

#### Scenario: Configured source `ifs` prunes the upper-case canonical directory
- **WHEN** the configured sources are `gfs, ifs` and both `raw/ifs/2026083012` and `canonical/IFS/2026083012` are older than the cutoff
- **THEN** the targets include `canonical/IFS/2026083012`
- **AND** no target path `canonical/ifs/...` is produced

#### Scenario: Grid definitions are preserved
- **WHEN** retention runs
- **THEN** `canonical/<storage_source>/grid/**` is never a target regardless of age

### Requirement: Precipitation PNG file cache is pruned on the mirror watermark
node-27 SHALL prune `NHMS_MVT_FILE_CACHE_DIR/precip/<storage_source>/<cycle_token>` in the same retention run and on the same cutoff that prunes `canonical/<storage_source>/<cycle_token>`, so a cycle whose mirror is gone cannot keep being served from rendered PNGs. The `<storage_source>/<cycle_token>` pair is byte-identical between the two trees, so the prune is a name-for-name mapping and needs no separate policy. The resulting cache inventory is bounded by `2 sources × 57 PNGs per cycle × kept cycles`.

#### Scenario: Pruned cycle stops serving cached PNGs
- **WHEN** the mirror directory `canonical/IFS/2026083012` is pruned and a PNG for that cycle was previously rendered and cached
- **THEN** the retention run also removes `NHMS_MVT_FILE_CACHE_DIR/precip/IFS/2026083012`
- **AND** a subsequent `GET /api/v1/precip/ifs/2026-08-30T12:00:00Z/<valid_time>.png` returns HTTP 404 `PRECIP_CYCLE_NOT_MIRRORED`, not a cache hit

#### Scenario: Cache inventory stays bounded
- **WHEN** retention has run and `K` cycles remain mirrored per source
- **THEN** `NHMS_MVT_FILE_CACHE_DIR/precip/**` holds at most `2 × 57 × K` PNG files, and no directory exists under it whose `<storage_source>/<cycle_token>` has no counterpart under `canonical/`
- **AND** the node-27 deployment receipt records the measured file count and `df -h` for the cache filesystem

#### Scenario: A kept cycle that borrowed from a pruned cycle degrades through the index
- **WHEN** a kept cycle's lead 0–21h windows borrowed slices from a cycle that has since been pruned
- **THEN** the index for the kept cycle stops listing those valid times (the resolver is evaluated against the mirror as it exists now), so the frontend hides the overlay for them by the existing index rule
- **AND** an already-cached PNG for such a valid time MAY still be served on a direct request, because it was rendered from a then-complete window; the cache is only invalidated when the cycle it belongs to is itself pruned

### Requirement: Mirror keep watermark covers every selectable cycle
The precipitation mirror keep set SHALL cover every cycle that `GET /api/v1/layers/discharge/cycles` can return for either source, plus the earlier cycles that the oldest listed cycle's lead-0 window borrows from — that is, `oldest_listed_cycle − 24h ≥ display_watermark − retention_days` MUST hold for both sources. The node-27 deployment receipt MUST record both sides of that inequality. If it does not hold, `retention_days` MUST be raised (deviation recorded in the receipt) rather than leaving the cycle selector offering cycles whose precipitation cannot be rendered.

#### Scenario: Receipt proves the coverage inequality
- **WHEN** the node-27 deployment receipt is produced
- **THEN** it lists, per source, the oldest cycle returned by the cycles endpoint, the retention cutoff `display_watermark − retention_days`, and the evaluated inequality `oldest_listed_cycle − 24h ≥ cutoff`

#### Scenario: Coverage violation is fixed by retention, not hidden
- **WHEN** the oldest listed cycle minus 24h falls before the retention cutoff
- **THEN** `retention_days` is raised so the inequality holds again, and the deviation is recorded in the receipt
- **AND** the frontend behaviour in the interim is the `PRECIP_CYCLE_NOT_MIRRORED` notice required by `precipitation-raster-overlay`, never a silent empty map
