## ADDED Requirements

### Requirement: q_down publish mirrors canonical precipitation products
After a q_down publish for `(source, cycle)` has completed its q_down copyback, artifact writes, and layer registration — that is, at the last step before the publish lineage is assembled — the publisher on node-22 SHALL mirror `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/` and every `canonical/<storage_source>/grid/<grid_id>/` directory that exists under the source root (storage source `gfs`/`IFS` via `normalize_source_id`, cycle token `%Y%m%d%H`) from `OBJECT_STORE_ROOT` to `NHMS_OBJECT_STORE_COPYBACK_ROOT` under the same keyspace using the existing temp-tree + rollback copy pattern with its own rollback batch, never the q_down batch.

The mirror is idempotent at **tree** granularity: a mirrored tree whose destination holds exactly the same file names with identical sizes is skipped without rewriting any file; any other state (missing file, extra file, size mismatch) replaces that whole tree atomically. Per-file skipping is not available because the reused copy helper rebuilds a temp tree and promotes it with a single `os.replace`.

The mirror MUST NOT fail, block, or roll back the q_down publish for **any** reason — missing source products, unsafe entry names, symlinks, tree/byte/depth limits, IO errors, or a failed rollback. Every such failure is swallowed and recorded in the publish lineage under the top-level key `precip_mirror` as `{"status": "failed", ...}`, carrying `missing_path` when the cause is an absent source directory and `error`/`error_type` otherwise. On success the key records `{"status": "ok", "file_count": <files mirrored across all trees>, "trees": [...]}`; when every tree was skipped it records `{"status": "skipped", ...}`.

The mirror runs only when the q_down copyback actually copied: when `NHMS_OBJECT_STORE_COPYBACK_ROOT` is unset the `precip_mirror` key is absent from the lineage, and when the copyback root resolves to the same directory identity as `OBJECT_STORE_ROOT` the key records `{"status": "skipped", "reason": "copyback_root_matches_object_store_root"}`.

The `<grid_id>` segment MUST NOT be derived by importing `workers/canonical_converter/converter.py` (its import chain needs third-party packages); both producers discover it by listing `canonical/<storage_source>/grid/*/` on the source root and mirroring each grid directory found. When `prcp_rate_or_amount` exists but no `grid.json` does, the status is `failed` with the absent grid path.

#### Scenario: Successful mirror
- **WHEN** q_down publish succeeds for `gfs` cycle `2026090212` and the source root holds 56 `.nc` files plus one `grid.json`
- **THEN** the copyback root contains the same 56 files and `grid.json` with identical bytes
- **AND** lineage records `precip_mirror.status == "ok"` with `file_count == 57` (both trees counted)

#### Scenario: Idempotent re-run
- **WHEN** the mirror runs again for a cycle whose every mirrored tree already holds the same file names with identical sizes
- **THEN** no file is rewritten (mtimes unchanged) and lineage records `precip_mirror.status == "skipped"`

#### Scenario: Partially mirrored tree is replaced whole
- **WHEN** the destination `prcp_rate_or_amount` tree is missing one `.nc` file or holds one of a different size
- **THEN** that whole tree is replaced atomically and lineage records `precip_mirror.status == "ok"`
- **AND** the grid tree, being unchanged, is still skipped

#### Scenario: Missing source does not block publish
- **WHEN** the source `prcp_rate_or_amount` directory is absent
- **THEN** q_down publish still completes with `status == "published"`
- **AND** lineage records `precip_mirror.status == "failed"` naming the absent path in `missing_path`

#### Scenario: Mirror failure other than a missing source does not block publish
- **WHEN** the source tree is unsafe or unreadable — a symlinked entry, an entry over `_COPYBACK_MAX_FILE_BYTES`, or an `OSError` raised mid-copy
- **THEN** q_down publish still completes with `status == "published"`
- **AND** lineage records `precip_mirror.status == "failed"` with `error` and `error_type`, and no temp tree is left under the copyback root

#### Scenario: Copyback root not configured
- **WHEN** `NHMS_OBJECT_STORE_COPYBACK_ROOT` is unset
- **THEN** no mirror is attempted and the lineage carries no `precip_mirror` key

### Requirement: One-shot backfill mirrors retained cycles without touching the environment
A script `scripts/canonical_precip_copyback_backfill.py` SHALL mirror every `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/` directory present under `--source-root` and each source's `canonical/<storage_source>/grid/*/grid.json` using only the standard library (it MUST NOT import `services`, `packages`, `workers`, or any third-party module — the keyspace rule is not shared with the publisher; the script copies the on-disk directory names verbatim and never normalizes a source id), runnable on node-22 as `/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill --source-root <root> --copyback-root <root>`; it MUST print a JSON summary (per cycle: copied/skipped/failed) to stdout. It writes by default and only `--dry-run` suppresses writes — the inverse of `services/tile_publisher/forcing_copyback_backfill.py`, because the node-22 operation in this change's tasks invokes it without a flag. Exit code: `0` when the run completes with no failure, `1` when the run completes but any cycle or grid reports `failed > 0` (the summary is still printed), `2` for unusable arguments or roots. Per file it skips a destination of identical size and otherwise copies through a temp name plus `os.replace`.

#### Scenario: Backfill summary
- **WHEN** the script runs against a source root with two sources and N cycles each
- **THEN** it exits 0 and prints a JSON summary listing every cycle with `copied`, `skipped`, and `failed` counts

#### Scenario: Dry run
- **WHEN** `--dry-run` is passed
- **THEN** no file and no directory is created under the copyback root and the summary reports the planned copies

#### Scenario: Failed cycle is signalled by the exit code
- **WHEN** one cycle cannot be mirrored (its source directory is unreadable) while others succeed
- **THEN** the summary is still printed with that cycle's `failed` count non-zero and the process exits 1

#### Scenario: Standard library only
- **WHEN** the script's imports are inspected, or it is run as `python -m scripts.canonical_precip_copyback_backfill` in a subprocess
- **THEN** it imports no `services`, `packages`, `workers`, or third-party module and runs to completion

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
