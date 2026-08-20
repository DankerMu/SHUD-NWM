# filesystem-permission-determinism

## ADDED Requirements

### Requirement: safe_fs directory creation pins an explicit base mode

Directory creation in `packages/common/safe_fs.py` SHALL pass an explicit base
mode of `0o755` to `os.mkdir`, so the landed permission is determined by the code
and the ambient umask together rather than by the umask alone. The helper SHALL
NOT `chmod` a directory after creating it, and SHALL NOT modify the mode of a
directory that already exists.

Consequently the ambient umask MAY further restrict a safe_fs-created directory
but SHALL NOT loosen it, and a safe_fs-created directory SHALL NOT carry a group-
or other-write bit under any umask.

#### Scenario: permissive umask no longer yields a group-writable directory

- **WHEN** the process umask is `0o002` and safe_fs creates a directory
- **THEN** the landed mode is `0o755`
- **AND** `stat.S_IMODE(mode) & 0o022` is `0`

#### Scenario: restrictive umask is preserved, not widened

- **WHEN** the process umask is `0o077` and safe_fs creates a directory
- **THEN** the landed mode is `0o700`, unchanged from the mode-less behavior
- **AND** the directory is not subsequently `chmod`-ed to a wider mode

#### Scenario: an existing directory keeps its mode

- **WHEN** safe_fs is asked to ensure a directory that already exists with mode
  `0o775`
- **THEN** the call succeeds without changing the directory's mode

### Requirement: the provider lock-parent gate stays fail-closed

`packages/common/provider_atomic.py` SHALL continue to reject a lock whose direct
parent directory is owned by another uid or carries any `0o022` bit, regardless of
which component created that parent. The gate SHALL NOT be relaxed to treat a uid
match as sufficient.

Callers that pre-create a directory which will become a provider lock parent are
responsible for creating it with a mode that satisfies this gate.

#### Scenario: pre-existing group-writable lock parent is refused

- **WHEN** a provider lock is requested whose direct parent already exists with
  mode `0o775`
- **THEN** the call raises `ProviderAtomicError("provider_lock_parent_unsafe")`
  in the `precommit` phase
- **AND** no lock file is created

#### Scenario: a state copyback parent keeps its explicit shared mode

- **WHEN** `_ensure_copyback_state_parent` creates a state copyback parent and
  widens the components it created to `0o775`
- **THEN** the copied checkpoint's parent directory is `0o775` and the checkpoint
  file is `0o664`
- **AND** the widening applies only to components created by that call
- **AND** it applies only to that parent — sibling copyback surfaces whose
  directories are created by safe_fs without a follow-up widening are not covered
  by this scenario

### Requirement: the directory-mode invariant is scoped to safe_fs

The invariant above SHALL be read as a property of directories created by
`packages/common/safe_fs.py`, not as a property of any directory tree. Other
production directory producers — an explicit wide `os.mkdir(..., 0o777)`, a bare
`Path.mkdir`, or `os.makedirs` — are outside this capability and keep their
current behavior.

Where a directory must be shared across uids, the sharing SHALL be established by
the caller after creation — an explicit `chmod`, or a permissive parent mode from
which the child's own mode is irrelevant. An inherited POSIX ACL on the parent is
**not** sufficient on its own: an explicit mode passed to `mkdir` clamps the
inherited ACL mask, so a named-user grant degrades to `#effective:r-x` on any
directory safe_fs creates. An ACL restores cross-uid write only when combined with
a post-creation `chmod` that widens the mask again.

#### Scenario: a non-safe_fs producer is unchanged

- **WHEN** a production code path creates a directory with an explicit wide mode
  or a bare `Path.mkdir` rather than through safe_fs
- **THEN** its landed mode is unchanged by this capability

### Requirement: permissive-umask behavior is covered by regression tests

The test suite SHALL cover the permissive umask side (`0o002`) symmetrically with
the existing strict side (`0o077`), pinning both the landed directory mode of a
safe_fs-created directory and successful provider lock acquisition under such a
parent.

Test helpers that pre-create provider lock parents SHALL create them with an
explicit mode rather than inheriting the ambient umask.

#### Scenario: the db-free scheduler suite is green under a permissive umask

- **WHEN** `tests/test_production_scheduler.py` runs with the process umask set
  to `0o002`
- **THEN** the run reports zero failures
