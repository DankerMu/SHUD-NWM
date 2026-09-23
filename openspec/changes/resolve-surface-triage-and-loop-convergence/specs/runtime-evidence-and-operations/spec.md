## ADDED Requirements

### Requirement: The scheduler root check SHALL attribute a symlink-loop root identically on every supported interpreter

`_scheduler_root_check` SHALL NOT use `Path.resolve()` to canonicalise the root under check or the workspace anchor, and SHALL NOT return early on an interpreter-specific exception with a separately assembled `check`. A root whose path is a symlink loop, lies below one, or folds onto one through `..` SHALL flow through the same assembly as any other root, so that the kernel `lstat` of the configured path decides the blocker (`SYMLINK`, `UNSAFE_PATH` on `ELOOP`, `NOT_FOUND` on `ENOENT`) and the `check` carries the same key set on CPython 3.11 and on CPython 3.13+. The verdict (`blocked`) for such roots is unchanged.

#### Scenario: A self-loop lock root yields the same code and keys on 3.11 and 3.14

- **GIVEN** a `ProductionSchedulerConfig` whose `lock_root` is a symlink pointing at itself
- **WHEN** the lock/evidence root preflight runs on CPython 3.11 and on CPython 3.14
- **THEN** both runs report `blocked` with blocker code `SCHEDULER_ROOT_LOCK_ROOT_SYMLINK` and the same `checks.lock_root` key set, including `symlink`, `allow_create` and `under_workspace`
