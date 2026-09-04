## ADDED Requirements

### Requirement: q_down publish mirrors canonical precipitation products
After a q_down publish for `(source, cycle)` has completed its q_down copyback, artifact writes, and layer registration — that is, at the last step before the publish lineage is assembled — the publisher on node-22 SHALL mirror `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/` and every `canonical/<storage_source>/grid/<grid_id>/` directory that exists under the source root (storage source `gfs`/`IFS` via `normalize_source_id`, cycle token `%Y%m%d%H`) from `OBJECT_STORE_ROOT` to `NHMS_OBJECT_STORE_COPYBACK_ROOT` under the same keyspace using the existing temp-tree + rollback copy pattern with its own rollback batch, never the q_down batch.

The mirror is idempotent at **tree** granularity: a mirrored tree whose destination holds exactly the same file names with identical sizes is skipped without rewriting any file; any other state (missing file, extra file, size mismatch) replaces that whole tree atomically. Per-file skipping is not available because the reused copy helper rebuilds a temp tree and promotes it with a single `os.replace`.

The mirror MUST NOT fail, block, or roll back the q_down publish for **any** reason — missing source products, unsafe entry names, symlinks, tree/byte/depth limits, IO errors, or a failed rollback. Every such failure is swallowed and recorded in the publish lineage under the top-level key `precip_mirror` as `{"status": "failed", ...}`, carrying `missing_path` when the cause is an absent source directory and `error`/`error_type` otherwise. On success the key records `{"status": "ok", "file_count": <files mirrored across all trees>, "trees": [...]}`; when every tree was skipped it records `{"status": "skipped", ...}`.

The `precip_mirror` payload MUST NOT claim work that did not happen, so each `trees[]` entry separates plan intent from outcome. `action` is the intent decided in the read-only plan phase and is always present: `copy` for a tree that needs replacing, `skip` for one whose destination already holds the same file names with identical sizes. `status` is the outcome and takes exactly five values: `skipped` (nothing to do), `pending` (a planned copy not yet promoted), `copied` (promoted, set only after the copy helper returned, with that tree's `file_count` **and** `byte_count` taken from the helper's returned counts rather than the plan-phase source stat, so the two numbers cannot describe different file sets), `rolled_back` (promoted and then undone by this step's own rollback, which returned cleanly), and `rollback_unknown` (promoted, this step's rollback raised, destination state unknown — the cycle MUST be re-mirrored before it is trusted).

A status MUST NOT be written at a point where the corresponding filesystem outcome is not knowable. Therefore, when this step's own rollback raises, **every** tree still at `copied` becomes `rollback_unknown`: the rollback helper collects per-entry errors and raises once at the end, so a tree it did undo is indistinguishable from one it did not, and even a tree whose own rollback raised may be absent from disk. The top-level `rollback_error`/`rollback_error_type` carry the cause. Trees never promoted stay `pending`. `file_count` is present on the `ok` record and on the `trees_already_mirrored` `skipped` record, and MUST be absent from a `failed` record (the same-root `skipped` record of the next paragraph carries none either); the top-level `file_count` is summed after the copy phase, so every `copied` tree contributes the copy helper's own count, while a `skipped` tree contributes its plan-phase count — the only honest number available for it, because nothing was written. `trees` is absent when the failure happened during planning, before the list was attached to the record.

The mirror runs only when the q_down copyback actually copied: when `NHMS_OBJECT_STORE_COPYBACK_ROOT` is unset the `precip_mirror` key is absent from the lineage, and when the copyback root resolves to the same directory identity as `OBJECT_STORE_ROOT` the key records `{"status": "skipped", "reason": "copyback_root_matches_object_store_root"}`.

The `<grid_id>` segment MUST NOT be derived by importing `workers/canonical_converter/converter.py` (its import chain needs third-party packages); both producers discover it by listing `canonical/<storage_source>/grid/*/` on the source root and mirroring each grid directory found. Discovery is directories-only, as `grid/*/` states: an entry under `canonical/<storage_source>/grid/` that is not a directory is not a tree to mirror and is ignored whatever its name, so it MUST NOT fail the mirror; a symlinked entry is still refused, and a *directory* whose name fails the safe-id rule is still refused by name. When `prcp_rate_or_amount` exists but no `grid.json` does, the status is `failed` with the absent grid path in `missing_path`. `missing_path` is used only where the path it names is genuinely absent: a `canonical/<storage_source>/grid/` that exists and lists successfully but holds no `<grid_id>` directory is reported with `error`/`error_type` instead, because the absent-`grid/` case emits that same path value and the two states would otherwise be indistinguishable in lineage — and because `missing_path` and `error`/`error_type` are mutually exclusive, the cause would be dropped entirely.

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
- **AND** lineage records `precip_mirror.status == "failed"` with `error` and `error_type`, and — when the record carries no `rollback_error` — no temp tree is left under the copyback root
- **AND** the record carries no `file_count`, and no tree reports `copied` unless the destination holds the source bytes

#### Scenario: A tree promoted before the batch failed is reported as rolled back
- **WHEN** the first mirrored tree has been promoted and a later tree's copy fails, so this step's own rollback runs
- **THEN** the promoted tree is removed (or restored byte-for-byte to the content the destination held before the run) and its `trees[]` entry reports `"action": "copy"` with `"status": "rolled_back"`, while a tree never attempted stays `"pending"`
- **AND** q_down publish still completes with `status == "published"` and its copyback products are untouched

#### Scenario: A rollback that itself raised reports an unknown destination state, never `copied`
- **WHEN** this step's own rollback runs and raises — the promoted tree was replaced by a symlink, or a restore failed between its `rmtree` and its `os.replace`, or one entry of a multi-tree batch failed while another was undone successfully
- **THEN** no tree reports `copied`: every tree that had been promoted reports `"status": "rollback_unknown"`, including any the rollback did undo and whose destination is therefore absent, while a tree never attempted stays `"pending"`
- **AND** lineage carries the top-level `rollback_error`/`rollback_error_type`, and a `.copyback-backup.<hex>` (or other `.copyback-*`) residue MAY be left under the copyback root — a restore that fails after its `rmtree` provably leaves one
- **AND** q_down publish still completes with `status == "published"` and its copyback products are untouched

#### Scenario: A non-directory beside the grid directories is ignored
- **WHEN** `canonical/<storage_source>/grid/` holds a plain file next to the `<grid_id>` directories, with a name the safe-id rule accepts or rejects
- **THEN** that entry is not mirrored and does not become a tree, and lineage records `precip_mirror.status == "ok"` with the `prcp_rate_or_amount` and `<grid_id>` trees mirrored as usual
- **AND** a *directory* under `canonical/<storage_source>/grid/` whose name the safe-id rule rejects is still `failed` with `error_type == "SafeFilesystemError"` naming the unsafe entry

#### Scenario: A grid directory with no grid found reports the cause, not a missing path
- **WHEN** `canonical/<storage_source>/grid/` exists and lists successfully but holds no `<grid_id>` directory
- **THEN** lineage records `precip_mirror.status == "failed"` with `error`/`error_type` and no `missing_path`, so it is distinguishable from an absent `canonical/<storage_source>/grid/`, which does record `missing_path`

#### Scenario: A tree's counts describe the copy phase, not the plan phase
- **WHEN** the converter writes one more lead into the source `prcp_rate_or_amount` tree between the read-only plan phase and the copy
- **THEN** that tree's `file_count` and `byte_count`, and the top-level `file_count`, all describe the file set the copy helper mirrored — which is what the copyback root then holds — never the smaller plan-phase measurement

#### Scenario: Copyback root not configured
- **WHEN** `NHMS_OBJECT_STORE_COPYBACK_ROOT` is unset
- **THEN** no mirror is attempted and the lineage carries no `precip_mirror` key

### Requirement: One-shot backfill mirrors retained cycles without touching the environment
A script `scripts/canonical_precip_copyback_backfill.py` SHALL mirror every `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/` directory present under `--source-root` and each source's `canonical/<storage_source>/grid/*/grid.json` using only the standard library (it MUST NOT import `services`, `packages`, `workers`, or any third-party module — the keyspace rule is not shared with the publisher; the script copies the on-disk directory names verbatim and never normalizes a source id), runnable on node-22 **from the repo root** as `cd /scratch/frd_muziyao/NWM && /scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill --source-root <root> --copyback-root <root>` (the `cd` is mandatory: from any other cwd the launch fails with `ModuleNotFoundError` and exit `1` — the same code as clause "completed but something failed", told apart only by the absent JSON summary); it MUST print a JSON summary (per cycle: copied/skipped/failed) to stdout. It writes by default and only `--dry-run` suppresses writes — the inverse of `services/tile_publisher/forcing_copyback_backfill.py`, because the node-22 operation in this change's tasks invokes it without a flag. Exit code: `0` when the run completes with no failure, `1` when the run completes but any cycle or grid reports `failed > 0` (the summary is still printed), `2` for unusable arguments or roots. A `canonical/` directory that exists under `--source-root` but is unreadable, is not a directory, or is a symlink IS an unusable root and exits `2` (the summary, carrying `root_error`, is still printed) — not `1`, because no cycle or grid entry exists for clause 1's predicate to be true of. An **absent** `canonical/` is not an error at all: there is nothing to mirror and the run exits `0`. Per file it skips a destination of identical size and otherwise copies through a temp name plus `os.replace`.

The script MUST NOT follow a symlinked tree **root**: `canonical/`, `canonical/<storage_source>/grid/` and `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/` are built as paths and are therefore `lstat`ed before being descended into, so a symlink there is recorded as a failure ("refusing to mirror a symlinked directory") rather than deep-copied out of `--source-root` into the shared copyback root. That refusal is one of three distinct rules and the spec claims no more than each enforces: (a) the three built tree roots above are `lstat`-rejected; (b) entries *inside* a tree are rejected as failures by the per-entry `S_ISLNK` check; (c) the directory names the script *discovers* by listing — `<storage_source>`, `<cycle_token>` and `<grid_id>` — are filtered by `entry.is_dir(follow_symlinks=False)`, so a symlink there is silently **skipped** and never appears in the summary at all. Destination-side path *components* under `--copyback-root` are NOT checked and ARE followed: the script `mkdir -p`s into the destination with no `lstat` or `O_NOFOLLOW` walk. That is a deliberate, recorded gap relative to the publisher (which walks its destination with an `O_NOFOLLOW` fd-walk plus a root component check, a rule a stdlib `lstat`-then-`mkdir` walk cannot match without a race) and not a privilege boundary, because `--copyback-root` is operator-supplied and anyone who can plant such a link already has write access to it. The per-file *leaf* is outside that gap and MUST stay so: the temp name `.<name>.backfill.<pid>.tmp` is opened `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`, so a symlink planted at it is refused and counted `failed` rather than followed out of the root (which would overwrite and widen an arbitrary outside file while still reporting the copy as `copied`), and a regular file already present at this run's own temp name is likewise refused, counted `failed` and unlinked. A temp left by a crashed run under a *different* pid does not collide with this run's temp name: it is neither refused nor swept, and the run reports it nowhere. A destination component that already exists under a name that is not a directory — a dangling symlink is enough, since the `exists()` probe follows it and reports absence — is likewise re-raised and recorded, never written through. Every directory the script creates under `--copyback-root` MUST be left group/world readable (`0o755`) and every file it promotes there MUST be left group/world readable (`0o644`), both applied with an explicit `chmod`/`fchmod` because `mkdir` and `O_CREAT` alike mask their mode argument with the process umask and `shutil.copyfile` does not carry permissions over, because node-22 writes as one account and node-27 reads the same NFS as another; directories the script did not create and files it did not write — an identical-size destination is skipped — are left alone with the mode they already had.

#### Scenario: Backfill summary
- **WHEN** the script runs against a source root with two sources and N cycles each
- **THEN** it exits 0 and prints a JSON summary listing every cycle with `copied`, `skipped`, and `failed` counts

#### Scenario: Dry run
- **WHEN** `--dry-run` is passed
- **THEN** no file and no directory is created under the copyback root and the summary reports the planned copies

#### Scenario: Failed cycle is signalled by the exit code
- **WHEN** one cycle cannot be mirrored (its source directory is unreadable) while others succeed
- **THEN** the summary is still printed with that cycle's `failed` count non-zero and the process exits 1

#### Scenario: A symlinked tree root is refused, not followed
- **WHEN** `canonical/`, `canonical/<storage_source>/grid/` or `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount/` is a symlink to a directory outside `--source-root`
- **THEN** the run exits non-zero (2 for `canonical/`, 1 for the other two), the summary reports the refusal with `failed > 0`, and no file from outside `--source-root` exists under `--copyback-root`

#### Scenario: Created directories and promoted files stay readable under a restrictive umask
- **WHEN** the script runs with a process umask of `0o077`
- **THEN** every directory it created under `--copyback-root`, intermediates included, is group/world readable and traversable
- **AND** every file it promoted there is group/world readable (`0o644`), so the reading account can open the products and not merely traverse to them
- **AND** a destination file the script did not write — skipped because its size already matches — keeps the mode it already had

#### Scenario: A partially created directory chain is still readable
- **WHEN** the `mkdir` of the leaf destination directory fails under a process umask of `0o077` after its ancestors have been created
- **THEN** those ancestors are already group/world readable in that same run (a single `mkdir(parents=True)` followed by a trailing `chmod` loop leaves them at the umask, because the loop is never reached)
- **AND** they are still group/world readable after a later clean rerun, which no longer counts them as newly created

#### Scenario: A dangling symlink at a precipitation tree root is refused, not read as "no products"
- **WHEN** `canonical/<storage_source>/<cycle_token>/prcp_rate_or_amount` is a symlink whose target does not exist
- **THEN** that cycle reports `status == "failed"` with the refusal message and the run exits 1 — never `status == "no_precip_products"` with exit 0

#### Scenario: A symlinked discovered directory name is skipped, not followed
- **WHEN** one of the directory names the script discovers by listing — `<storage_source>`, `<cycle_token>` or `<grid_id>` — is a symlink to a directory outside `--source-root`
- **THEN** that name is silently skipped: it appears in no cycle or grid entry of the summary, the run's `failed` total is unchanged, and no file from outside `--source-root` exists under `--copyback-root`

#### Scenario: A symlink planted at the per-file temp name is refused
- **WHEN** `.<name>.backfill.<pid>.tmp` in a destination directory is a symlink to a file outside `--copyback-root`
- **THEN** that file is recorded `failed` and not `copied`, the outside file keeps its bytes and its mode, and no symlink is promoted into the mirror

#### Scenario: Unusable canonical root exits 2 while an absent one exits 0
- **WHEN** `canonical/` under `--source-root` is a regular file or is unreadable
- **THEN** the summary is printed with `root_error` and the process exits 2
- **AND** when `canonical/` is simply absent the run exits 0 with empty `cycles` and `grids`

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
