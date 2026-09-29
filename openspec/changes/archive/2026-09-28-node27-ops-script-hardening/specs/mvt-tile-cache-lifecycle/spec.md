## MODIFIED Requirements

### Requirement: MVT file cache retention prunes exactly three path shapes by wall-clock mtime

`scripts/node27_mvt_cache_retention.py` SHALL be a stdlib-only runner that never opens a database connection. It reads the cache root from `NHMS_MVT_FILE_CACHE_DIR` (the value the display API PROCESS has), expanded with `Path(...).expanduser()` exactly as `services/tiles/mvt.py` does, and the age from `NODE27_MVT_CACHE_RETENTION_DAYS` (default 14; `--retention-days` overrides). The age MUST parse as an integer ≥ 1 from whichever source supplies it; a non-integer or `< 1` value is a preflight blocker, never silently replaced by the default (a deliberate departure from `node27_raw_retention._env_int`). The cache root MUST be absolute, MUST NOT be `/`, MUST exist, MUST NOT be a symlink (checked with `is_symlink()` on the unresolved, `expanduser()`-ed path BEFORE any `resolve()`), and MUST be a directory; enumeration uses that unresolved path. A malformed `--reference-time` (not RFC3339) is a preflight blocker with `field: reference_time`, `reason: not_rfc3339`. `<root>/.locks` MUST itself be a non-symlink directory for the lock lane to run; otherwise that lane alone is skipped with a lane-level `skipped[]` entry and the tile/intermediate lane still runs. The cutoff is `reference_time − retention_days` where `reference_time` is the wall clock at start (UTC) or the RFC3339 `--reference-time` argument; it is deliberately NOT the display watermark, because a pruned tile is regenerated from the database on the next request (a cache miss), whereas the precipitation PNG lane's input is an irreplaceable mirror.

A file is a target only when ALL hold: its `lstat` says regular file (never a symlink, directory, or other type); its parent directory is a non-symlink directory named exactly `[0-9a-f]{2}` directly under the cache root (tile bodies and write intermediates) or directly under `<root>/.locks` (lock files); its name matches exactly one of `[0-9a-f]{64}\.pbf`, `\.[0-9a-f]{64}\.pbf\.[0-9]+\.tmp` (the write intermediate `_write_file_cache` leaves behind on a crash), or `[0-9a-f]{64}\.lock` (in `.locks/<hh>/` only); and `st_mtime < cutoff`. The `.pbf` lane MUST NOT enumerate, stat, or delete anything else: `<root>/precip/**`, `<root>/basemap/**`, unknown directories, files at the root, or deeper levels. The `<root>/basemap/tianditu` subtree is touched only by the basemap stage defined in "The shared Tianditu basemap cache is refreshed on hit and pruned by age". A lock file is unlinked only while the runner holds a non-blocking `flock(LOCK_EX | LOCK_NB)` on a descriptor obtained with `os.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)` — never with `O_CREAT`, so a deleter cannot recreate what it is removing, and with `O_NONBLOCK` so a FIFO swapped into the path can never block the runner; `ENOENT` at open is `already_gone`, a busy lock is recorded as skipped with reason `lock_held`, never deleted. The order inside one lock target is fixed: open → `fstat` → `flock(LOCK_NB)` → `lstat` identity → unlink. An `OSError` raised by that `fstat` (for example `ESTALE`/`EIO` on NFS) is classified like every other step after the open: the target is recorded in `failed[]` with its `error` and `error_type`, the remaining targets are still processed, and the summary is still written — an exception MUST NOT escape `run_retention` and leave the run without a summary. A lock path that `O_NOFOLLOW` refuses (`ELOOP`/`EMLINK`) or whose descriptor `fstat` says is not a regular file is skipped with reason `not_regular_file` BEFORE any `flock` is attempted, so a directory or FIFO is classified without ever taking a lock. After the `flock` succeeds and before unlinking, the runner MUST compare `(st_dev, st_ino)` of `fstat(fd)` with `lstat(path)`; a mismatch or `ENOENT` means the path was recreated by a live miss since the open, and the target is recorded as `already_gone` without unlinking. Tile bodies and intermediates are unlinked directly without being opened. Lane-level skips use `locks_root_missing` (an absent `.locks`, the normal state of a fresh cache root) or `locks_root_unsafe` with `detail` `path_is_symlink` / `path_not_directory` / `path_unavailable` (the `lstat` of `.locks` itself raised, e.g. an unreadable root). An `OSError` from enumerating the cache root, `<root>/.locks`, or any `<hh>` directory (`EACCES`, `ESTALE`, …) is NOT a silent empty lane: it is recorded in `failed[]` as `{path, kind: null, reason: enumeration_unavailable, error, error_type}`, the other directories are still processed, and the run exits 1, so the health criterion goes red instead of reporting a clean zero. A single entry that vanishes between `scandir` and its `stat` is the ordinary concurrent-miss race and is skipped silently. The same race one level up is exempt too: a `<hh>` directory directly under the cache root or directly under `<root>/.locks` that is removed, or replaced by a non-directory, between its parent's listing and its own `scandir` (`FileNotFoundError` / `NotADirectoryError`) contributes no targets and no `failed[]` entry. The exemption is limited to those two errnos on `<hh>` directories: the cache root itself and `<root>/.locks` itself are never exempt, and every other `OSError` on a `<hh>` directory (`EACCES`, `ESTALE`, …) stays `enumeration_unavailable`.

#### Scenario: Aged tile, intermediate and lock files are pruned; everything else survives

- **WHEN** the cache root holds an aged `ab/<sha>.pbf`, an aged `ab/.<sha>.pbf.123.tmp`, an aged `.locks/ab/<sha>.lock`, a fresh `cd/<sha2>.pbf`, an aged `precip/IFS/2026060100/x.png`, an aged `zz/<sha>.pbf` (parent not hex), an aged `ab/notes.txt`, an aged symlink `ab/<sha3>.pbf` → elsewhere, an aged `ab/<sha4>.pbf` that is a directory, and an aged `ab/cd/<sha5>.pbf` one level too deep
- **THEN** with `NODE27_MVT_CACHE_RETENTION_PLAN_ONLY` unset the three aged shaped files are deleted and every other path still exists afterwards
- **AND** no entry of `planned[]`, `deleted[]`, `failed[]` or `skipped[]` names a path under `<root>/precip/`

#### Scenario: An unreadable hex directory fails the run while its siblings still prune

- **WHEN** `<root>/ab/` is unreadable (mode `0o000`, not running as root) and `<root>/cd/<sha>.pbf` is aged
- **THEN** `failed[]` holds exactly one entry for `<root>/ab` with `kind: null`, `reason: enumeration_unavailable` and `error_type: PermissionError`, the `cd/` tile is still deleted, the exit code is 1, and the same failure entry is reported (with exit 1) in plan-only mode

#### Scenario: Non-regular replacements at a collected lock path are classified, never locked or deleted

- **WHEN** an aged `.locks/ab/<sha>.lock` is collected and, before the runner opens it, is replaced by a symlink, a directory, or a FIFO with no writer
- **THEN** the target is recorded as `skipped[]` with reason `not_regular_file` and `kind: lock`, the replacement still exists afterwards, and the runner returns promptly (the FIFO never blocks the open)

#### Scenario: Plan-only lists the same targets and removes nothing

- **WHEN** `NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=true`
- **THEN** `planned[]` holds the same targets the execute mode would delete, `deleted[]` is empty, `execution_mode` is `plan_only`, every file still exists, and the process exits 0

#### Scenario: A held lock file is skipped, not deleted

- **WHEN** an aged `.locks/ab/<sha>.lock` is currently `flock`-ed by another process
- **THEN** the runner records it in `skipped[]` with reason `lock_held`, does not unlink it, and the run still completes with the other targets deleted

#### Scenario: Missing or unsafe cache root blocks before any deletion

- **WHEN** `NHMS_MVT_FILE_CACHE_DIR` is unset, or names a path that after `expanduser()` is relative, is `/`, is absent, is a symlink, or is not a directory, or `NODE27_MVT_CACHE_RETENTION_DAYS` / `--retention-days` is not an integer ≥ 1 (including `--retention-days 0`), or `--reference-time` is not RFC3339
- **THEN** the runner writes a `preflight_blocked` summary whose `blockers[]` entries name the failing `field` and `reason` to the same sink a completed run would use (`--summary-path` when given, else stdout), and exits 2 without touching any file

#### Scenario: A symlinked `.locks` directory retires only the lock lane

- **WHEN** `<root>/.locks` is a symlink to a directory outside the cache root holding aged `ab/<sha>.lock` files, and `<root>/ab/<sha2>.pbf` is aged
- **THEN** the `.pbf` is deleted, no `.lock` under the symlink target is touched, and `skipped[]` carries one lane-level entry (`kind: null`, reason `locks_root_unsafe`, detail `path_is_symlink`) for `.locks`

#### Scenario: A lock path recreated after the runner opened it is not unlinked

- **WHEN** the runner has opened an aged `.locks/ab/<sha>.lock` and, before it unlinks, the path is replaced by a different inode (a live miss recreated it)
- **THEN** the runner records `already_gone` for that target and the replacement file is untouched

#### Scenario: A hex directory removed mid-run is the concurrent-removal race, not a failure

- **WHEN** `<root>/ab/` (or `<root>/.locks/ab/`) is listed by its parent and then raises `FileNotFoundError` or `NotADirectoryError` on its own `scandir`, and `<root>/cd/<sha>.pbf` is aged
- **THEN** `failed[]` is empty, the `cd/` tile is deleted, and the exit code is 0

#### Scenario: An unreadable `.locks` directory fails the run while the tile lane still prunes

- **WHEN** `<root>/.locks` is a real directory whose `lstat` succeeds but whose `scandir` raises `PermissionError`, and `<root>/cd/<sha>.pbf` is aged
- **THEN** `failed[]` holds exactly one `enumeration_unavailable` entry for `<root>/.locks`, the `cd/` tile is still deleted, and the exit code is 1

#### Scenario: A failing `fstat` on a lock target is recorded, and the summary is still written

- **WHEN** `os.fstat` on an opened aged `.locks/ab/<sha>.lock` descriptor raises `OSError(ESTALE)`
- **THEN** the summary JSON is written to `--summary-path` with `counts.failed == 1` and that target's `error_type` `OSError`, the descriptor is closed, and the exit code is 1

#### Scenario: Post-lock identity-check and unlink errors are classified

- **WHEN** after the `flock` succeeds the `lstat` of the lock path raises an `OSError` other than `ENOENT`
- **THEN** the target is recorded in `failed[]`
- **WHEN** the final `unlink` raises `FileNotFoundError`
- **THEN** the target is recorded as `already_gone`, never in `failed[]`

### Requirement: MVT cache retention summary and exit codes mirror the raw-retention runner

The runner SHALL write a JSON summary to `--summary-path` on every non-blocked run with `schema_version`, `started_at`, `finished_at`, `reference_time`, `cache_root`, `retention_days`, `cutoff`, `enabled`, `plan_only`, `execution_mode` (`disabled` | `plan_only` | `production_execute`), `status`, `counts` (`planned`, `deleted`, `skipped`, `failed`), `planned[]`, `deleted[]` (each entry: `path`, `kind` ∈ {`pbf`, `tmp`, `lock`}, `size_bytes`, `mtime` as an RFC3339 UTC seconds-precision string), `skipped[]` (each: `path`, `kind` — the same enum, or `null` for a lane-level skip — and `reason`), `failed[]` (each: `path`, `kind`, `error`, `error_type`; a directory that could not be enumerated carries `kind: null` plus `reason: enumeration_unavailable`), `freed_bytes`, `precip_root_untouched` (the literal `<cache_root>/precip` path this runner never enters), and a `basemap` object for the basemap stage (`mode` ∈ {`disabled`, `dry_run`, `delete`}, `retention_days`, per-kind counts for `tile` and `tmp`, removed empty directories, skipped symlinks, and its own `failed[]`); basemap entries use `kind` ∈ {`basemap_tile`, `basemap_tmp`, `basemap_dir`} and never appear in the `.pbf` lane's lists. When `--summary-path` is not given the same JSON is printed to stdout; the runner reads no environment-variable fallback for the summary sink (the wrapper always passes the flag), and a relative `--summary-path` is a valid sink resolved against the working directory. Exit code SHALL be 0 when both `failed[]` and `basemap.failed[]` are empty, 1 when either is not, and 2 when preflight blocked. `NODE27_MVT_CACHE_RETENTION_ENABLED=false` yields `execution_mode: disabled` with zero targets and exit 0. A target that disappears between planning and unlink (`FileNotFoundError`) is moved to `skipped[]` with reason `already_gone`, never to `failed[]`.

#### Scenario: Disabled gate yields a disabled summary

- **WHEN** `NODE27_MVT_CACHE_RETENTION_ENABLED=false`
- **THEN** the summary has `execution_mode: disabled`, all counts 0, and the exit code is 0

#### Scenario: A file removed by a concurrent writer is not a failure

- **WHEN** a planned target no longer exists at unlink time
- **THEN** it appears in `skipped[]` with reason `already_gone`, `failed[]` stays empty, and the exit code is 0

## ADDED Requirements

### Requirement: The shared Tianditu basemap cache is refreshed on hit and pruned by age

The display API SHALL refresh a basemap tile's mtime (`os.utime`, times unset) on a cache hit when the file is older than one day, swallowing any `OSError` so the hit is still served. The retention runner SHALL, as a separate basemap stage under the same gates, remove `<root>/basemap/tianditu/<layer>/<z>/<x>/<y>` tiles (numeric `z`, `x`, `y`; known layer) whose mtime is older than `NODE27_MVT_CACHE_RETENTION_BASEMAP_DAYS` (default 30, a positive integer or a preflight blocker), NWM (`.<y>.<pid>.<tid>.<hex>.tmp`) and yd (`tmp-<32hex>`) temporary files older than one day, and then empty directories below the layer level; unknown names and symlinks SHALL be kept. The stage SHALL delete only when `NODE27_MVT_CACHE_RETENTION_BASEMAP_DELETE` is exactly `1`; otherwise it reports `mode: dry_run` counts. `NODE27_MVT_CACHE_RETENTION_ENABLED=false` and `NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=true` take precedence over the basemap switch. An absent `basemap/tianditu` subtree SHALL NOT block the `.pbf` lane.

#### Scenario: Cold tile after retention

- **WHEN** deletion is enabled and a tile under `basemap/tianditu/<layer>/<z>/<x>/<y>` has an mtime older than the retention
- **THEN** the stage SHALL delete it, keep newer tiles, unknown file names and symlinks, and report the counts

#### Scenario: Deletion not enabled

- **WHEN** the stage runs without the explicit deletion switch, or under plan-only
- **THEN** it SHALL delete nothing and report the counts it would delete

#### Scenario: Invalid basemap retention

- **WHEN** `NODE27_MVT_CACHE_RETENTION_BASEMAP_DAYS` is not a positive integer
- **THEN** the runner SHALL block at preflight with a JSON summary and exit 2

#### Scenario: A basemap stage failure turns the run red

- **WHEN** the basemap stage cannot remove or enumerate an entry (for example a `PermissionError` on a directory another uid wrote)
- **THEN** the entry SHALL be listed in `basemap.failed[]`, the runner SHALL exit 1, and the `.pbf` lane SHALL still prune its own targets

#### Scenario: Hit refresh

- **WHEN** a cached basemap tile older than one day is served
- **THEN** its mtime SHALL be refreshed, a tile younger than one day SHALL NOT be touched, and a `PermissionError` from the refresh SHALL NOT change the response
