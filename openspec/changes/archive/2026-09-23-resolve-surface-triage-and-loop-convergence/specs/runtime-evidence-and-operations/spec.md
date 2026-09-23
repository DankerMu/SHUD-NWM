## ADDED Requirements

### Requirement: The scheduler root check SHALL attribute a symlink-loop root identically on every supported interpreter

`_scheduler_root_check` SHALL NOT use `Path.resolve()` to canonicalise the root under check or the workspace anchor, and SHALL NOT answer a symlink-loop root with an early return on an interpreter-specific exception and a separately assembled `check`. A root whose path is a symlink loop, lies below one, or folds onto one through `..` SHALL flow through the same assembly as any other root, so that the kernel `lstat` of the configured path decides the blocker (`SYMLINK`, `UNSAFE_PATH` on `ELOOP`, `NOT_FOUND` on `ENOENT`) and the `check` carries the same key set on CPython 3.11 and on CPython 3.13+. The verdict (`blocked`) for such roots is unchanged. A symlink CHAIN (no loop) deeper than the interpreter's recursion limit, which up to CPython 3.12 makes `os.path.realpath` raise `RecursionError`, SHALL yield a `blocked` check with a typed `UNSAFE_PATH` blocker instead of an exception; on 3.13+ the chain resolves and the `lstat` decides. The pass that renders a blocked root into `resolved_runtime_roots` SHALL NOT abort on a loop root: that path string is render-only and SHALL NOT be produced with `Path.resolve()`.

#### Scenario: A self-loop lock root yields the same code and keys on 3.11 and 3.14

- **GIVEN** a `ProductionSchedulerConfig` whose `lock_root` is a symlink pointing at itself
- **WHEN** the lock/evidence root preflight runs on CPython 3.11 and on CPython 3.14
- **THEN** both runs report `blocked` with blocker code `SCHEDULER_ROOT_LOCK_ROOT_SYMLINK` and the same `checks.lock_root` key set, including `symlink`, `allow_create` and `under_workspace`

#### Scenario: A full scheduler pass with a loop lock root writes its blocked evidence on 3.11 and 3.14

- **GIVEN** runtime roots are required and the configured `lock_root` is a symlink pointing at itself (or lies below one)
- **WHEN** `ProductionScheduler.from_env(config).run_once()` runs on CPython 3.11 and on CPython 3.14
- **THEN** neither run raises; both return `preflight_blocked` and write a pass artifact under the evidence root whose `root_preflight.blockers` carry `SCHEDULER_ROOT_LOCK_ROOT_SYMLINK` (or `..._UNSAFE_PATH` for the below-loop root) and whose `resolved_runtime_roots.lock_root.path` is the configured spelling

#### Scenario: A symlink chain deeper than the recursion limit is blocked, not raised

- **GIVEN** a root that is the head of a finite symlink chain longer than `sys.getrecursionlimit()`
- **WHEN** `_scheduler_root_check` runs on it
- **THEN** it returns a `blocked` check on every interpreter: `UNSAFE_PATH` with the canonicaliser-failure key set on CPython 3.11, `SYMLINK` with the ordinary-symlink key set on CPython 3.13+
