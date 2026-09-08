# Spec: mvt-tile-cache-lifecycle

## Purpose

Bound the lifetime of every file the display API creates under `NHMS_MVT_FILE_CACHE_DIR` outside `precip/`: tile bodies and write intermediates age out on a wall-clock cutoff through a dedicated retention runner, and a tile generation lock file exists only while one in-flight cache miss holds it.

## ADDED Requirements

### Requirement: MVT file cache retention prunes exactly three path shapes by wall-clock mtime

`scripts/node27_mvt_cache_retention.py` SHALL be a stdlib-only runner that never opens a database connection. It reads the cache root from `NHMS_MVT_FILE_CACHE_DIR` (the value the display API PROCESS has), expanded with `Path(...).expanduser()` exactly as `services/tiles/mvt.py` does, and the age from `NODE27_MVT_CACHE_RETENTION_DAYS` (default 14; `--retention-days` overrides). The age MUST parse as an integer ≥ 1 from whichever source supplies it; a non-integer or `< 1` value is a preflight blocker, never silently replaced by the default (a deliberate departure from `node27_raw_retention._env_int`). The cache root MUST be absolute, MUST NOT be `/`, MUST exist, MUST NOT be a symlink (checked with `is_symlink()` on the unresolved, `expanduser()`-ed path BEFORE any `resolve()`), and MUST be a directory; enumeration uses that unresolved path. A malformed `--reference-time` (not RFC3339) is a preflight blocker with `field: reference_time`, `reason: not_rfc3339`. `<root>/.locks` MUST itself be a non-symlink directory for the lock lane to run; otherwise that lane alone is skipped with a lane-level `skipped[]` entry and the tile/intermediate lane still runs. The cutoff is `reference_time − retention_days` where `reference_time` is the wall clock at start (UTC) or the RFC3339 `--reference-time` argument; it is deliberately NOT the display watermark, because a pruned tile is regenerated from the database on the next request (a cache miss), whereas the precipitation PNG lane's input is an irreplaceable mirror.

A file is a target only when ALL hold: its `lstat` says regular file (never a symlink, directory, or other type); its parent directory is a non-symlink directory named exactly `[0-9a-f]{2}` directly under the cache root (tile bodies and write intermediates) or directly under `<root>/.locks` (lock files); its name matches exactly one of `[0-9a-f]{64}\.pbf`, `\.[0-9a-f]{64}\.pbf\.[0-9]+\.tmp` (the write intermediate `_write_file_cache` leaves behind on a crash), or `[0-9a-f]{64}\.lock` (in `.locks/<hh>/` only); and `st_mtime < cutoff`. The runner MUST NOT enumerate, stat, or delete anything else: `<root>/precip/**`, unknown directories, files at the root, or deeper levels. A lock file is unlinked only while the runner holds a non-blocking `flock(LOCK_EX | LOCK_NB)` on a descriptor obtained with `os.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)` — never with `O_CREAT`, so a deleter cannot recreate what it is removing, and with `O_NONBLOCK` so a FIFO swapped into the path can never block the runner; `ENOENT` at open is `already_gone`, a busy lock is recorded as skipped with reason `lock_held`, never deleted. The order inside one lock target is fixed: open → `fstat` → `flock(LOCK_NB)` → `lstat` identity → unlink. A lock path that `O_NOFOLLOW` refuses (`ELOOP`/`EMLINK`) or whose descriptor `fstat` says is not a regular file is skipped with reason `not_regular_file` BEFORE any `flock` is attempted, so a directory or FIFO is classified without ever taking a lock. After the `flock` succeeds and before unlinking, the runner MUST compare `(st_dev, st_ino)` of `fstat(fd)` with `lstat(path)`; a mismatch or `ENOENT` means the path was recreated by a live miss since the open, and the target is recorded as `already_gone` without unlinking. Tile bodies and intermediates are unlinked directly without being opened. Lane-level skips use `locks_root_missing` (an absent `.locks`, the normal state of a fresh cache root) or `locks_root_unsafe` with `detail` `path_is_symlink` / `path_not_directory` / `path_unavailable` (the `lstat` of `.locks` itself raised, e.g. an unreadable root). An `OSError` from enumerating the cache root, `<root>/.locks`, or any `<hh>` directory (`EACCES`, `ESTALE`, …) is NOT a silent empty lane: it is recorded in `failed[]` as `{path, kind: null, reason: enumeration_unavailable, error, error_type}`, the other directories are still processed, and the run exits 1, so the health criterion goes red instead of reporting a clean zero. A single entry that vanishes between `scandir` and its `stat` is the ordinary concurrent-miss race and is skipped silently.

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

### Requirement: MVT cache retention summary and exit codes mirror the raw-retention runner

The runner SHALL write a JSON summary to `--summary-path` on every non-blocked run with `schema_version`, `started_at`, `finished_at`, `reference_time`, `cache_root`, `retention_days`, `cutoff`, `enabled`, `plan_only`, `execution_mode` (`disabled` | `plan_only` | `production_execute`), `status`, `counts` (`planned`, `deleted`, `skipped`, `failed`), `planned[]`, `deleted[]` (each entry: `path`, `kind` ∈ {`pbf`, `tmp`, `lock`}, `size_bytes`, `mtime` as an RFC3339 UTC seconds-precision string), `skipped[]` (each: `path`, `kind` — the same enum, or `null` for a lane-level skip — and `reason`), `failed[]` (each: `path`, `kind`, `error`, `error_type`; a directory that could not be enumerated carries `kind: null` plus `reason: enumeration_unavailable`), `freed_bytes`, and `precip_root_untouched` (the literal `<cache_root>/precip` path this runner never enters). When `--summary-path` is not given the same JSON is printed to stdout; the runner reads no environment-variable fallback for the summary sink (the wrapper always passes the flag), and a relative `--summary-path` is a valid sink resolved against the working directory. Exit code SHALL be 0 when `failed[]` is empty, 1 when it is not, and 2 when preflight blocked. `NODE27_MVT_CACHE_RETENTION_ENABLED=false` yields `execution_mode: disabled` with zero targets and exit 0. A target that disappears between planning and unlink (`FileNotFoundError`) is moved to `skipped[]` with reason `already_gone`, never to `failed[]`.

#### Scenario: Disabled gate yields a disabled summary

- **WHEN** `NODE27_MVT_CACHE_RETENTION_ENABLED=false`
- **THEN** the summary has `execution_mode: disabled`, all counts 0, and the exit code is 0

#### Scenario: A file removed by a concurrent writer is not a failure

- **WHEN** a planned target no longer exists at unlink time
- **THEN** it appears in `skipped[]` with reason `already_gone`, `failed[]` stays empty, and the exit code is 0

### Requirement: The retention runner is deployed as a user-level systemd oneshot with a daily timer

`infra/systemd/nhms-node27-mvt-cache-retention.service` SHALL declare `ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-mvt-cache-retention-logs` before `ExecStart`, append-mode `StandardOutput`/`StandardError` under that directory, and `ExecStart=/home/nwm/NWM/scripts/node27_mvt_cache_retention_once.sh`; `infra/systemd/nhms-node27-mvt-cache-retention.timer` SHALL fire daily with `Persistent=true`. The wrapper SHALL refuse an env file that is missing, a symlink, or not mode 0600, SHALL hold a non-blocking `flock` so overlapping ticks skip, and SHALL propagate the runner's exit code unchanged. `infra/env/node27-mvt-cache-retention.example` SHALL carry `NHMS_MVT_FILE_CACHE_DIR` with the warning that it must equal the display PROCESS's value (the `nhms-display-api.service` ExecStart default `/home/nwm/.cache/nhms/mvt`, not `display.example`'s `/tmp/...`).

#### Scenario: Unit file bootstraps its log directory before ExecStart

- **WHEN** a test reads the service unit
- **THEN** the `ExecStartPre` mkdir line index is strictly less than the `ExecStart` line index and the `StandardOutput=append:` path lives under the same directory

#### Scenario: Wrapper refuses an unsafe env file, skips an overlapping tick, and propagates the runner's exit code

- **WHEN** the wrapper runs with an env file that is missing, a symlink, or mode 0640
- **THEN** it exits 2 and logs the blocked reason (`ENV_FILE_MISSING`, `ENV_FILE_SYMLINK_FORBIDDEN`, or `ENV_FILE_MODE_UNSAFE`) without invoking the runner
- **WHEN** another process holds the wrapper's `flock`
- **THEN** it logs the skip and exits 0
- **WHEN** the runner exits 1 or 2
- **THEN** the wrapper exits with that same code

### Requirement: A tile generation lock file lives only while one cache miss holds it

`services/tiles/mvt.py::tile_generation_lock` SHALL, on exit from the guarded block (normal return or exception from the producer, including the 424/413/500 paths), unlink its lock file BEFORE releasing the `flock`, so no waiter can acquire the lock and then observe the path removed underneath it. On acquisition it SHALL, after `flock(LOCK_EX)` succeeds, compare `(st_dev, st_ino)` from `fstat` of the held descriptor with `stat` of the lock path, through the module-level probes `_lock_file_identity(fd)` and `_lock_path_identity(path)` (the latter returns `None` on `ENOENT`); on mismatch or `None` it SHALL close the descriptor and try again, where one attempt is open + `flock` + compare and `_TILE_LOCK_REACQUIRE_LIMIT` is the total number of attempts (so `_lock_path_identity` is called exactly that many times before giving up); after the last failed attempt it logs a warning and proceeds without the cross-process lock rather than spinning or hanging, with every descriptor it opened closed; an exception raised inside an attempt after the `flock` (for example a `PermissionError` from the path probe) closes that attempt's descriptor — releasing the lock — before it propagates. Exhaustion needs no external actor: every holder handover (unlink then release) costs each waiter blocked on that inode one attempt, so it is reachable with at least `_TILE_LOCK_REACQUIRE_LIMIT + 1` same-key cross-process contenders, i.e. it is coupled to `NHMS_DISPLAY_WORKERS`, and the degrade is one duplicate generation. The in-process `_LOCAL_TILE_LOCKS` is and remains a `weakref.WeakValueDictionary`, whose entry for a key disappears when the last holder or waiter of that key drops its reference — this clause pins EXISTING behavior (bounded by in-flight misses, not by keys ever missed) and introduces no change. Single-flight semantics are unchanged: two concurrent misses for one key in two processes serialize, the second re-reads the cache inside the lock and serves the first's bytes.

#### Scenario: Lock file is gone after the miss completes

- **WHEN** a cold miss for key K is generated under `tile_generation_lock`, whether the producer returns or raises
- **THEN** `<root>/.locks/<K[:2]>/<K>.lock` does not exist after the context exits (new behavior), and once every reference to the guarded block has been dropped `K` is absent from `_LOCAL_TILE_LOCKS` (existing `WeakValueDictionary` behavior, pinned)

#### Scenario: The lock file is unlinked before the lock is released

- **WHEN** a miss for key K completes and `fcntl.flock` is observed
- **THEN** at the moment `LOCK_UN` is issued the lock path no longer exists

#### Scenario: A probe failure after the lock is taken releases the descriptor

- **WHEN** `_lock_path_identity` raises an `OSError` other than `FileNotFoundError` while an attempt holds the `flock`
- **THEN** the exception propagates, the attempt's descriptor is closed before it does (a fresh non-blocking `flock` on the path succeeds), and the process's open descriptor count is unchanged

#### Scenario: Two processes contend for one key and stay single-flight

- **WHEN** process A holds the lock for K and process B enters `tile_generation_lock(K)` in a separate process
- **THEN** B acquires only after A has unlinked and released, B's guarded block starts after A's ends (no overlap), B re-acquires on a freshly created inode when the one it blocked on was unlinked, and no lock file remains once both exit

#### Scenario: Retry exhaustion degrades to duplicate work, never a hang

- **WHEN** `_lock_path_identity` keeps reporting an identity different from the held descriptor's
- **THEN** after exactly `_TILE_LOCK_REACQUIRE_LIMIT` probe calls, with the process's open descriptor count unchanged from before the call, the block runs without the cross-process lock and a warning is logged

#### Scenario: Legacy and identity national routes serve unchanged bytes

- **WHEN** a master-baseline instance and a change-head instance run side by side against one database, each with an empty file cache, and the same z4 tile is requested cold on each through the legacy 5-segment `hydro-national` route and through the `{source}/{cycle}` route for `gfs` and `ifs`
- **THEN** every pair returns the same HTTP status and the same `ETag` (`W/"m16-<sha256(body)>"`, equivalently `X-Tile-Checksum`); byte counts are recorded as a secondary datum

### Requirement: The raw-retention precip lane and the MVT cache retention never touch each other's subtree

`scripts/node27_raw_retention.py` SHALL continue to descend only into `<root>/precip/`, and `scripts/node27_mvt_cache_retention.py` MUST never descend into `<root>/precip/`. The two runners share the `NHMS_MVT_FILE_CACHE_DIR` value and nothing else.

#### Scenario: Mirror-image exclusion holds on one shared root

- **WHEN** one cache root holds an aged `precip/IFS/2026060100/*.png` and an aged `ab/<sha>.pbf`
- **THEN** the raw-retention runner plans only the `precip/...` directory and the MVT cache runner plans only the `.pbf`, and neither lists the other's path under any key
