## MODIFIED Requirements

### Requirement: The safe-directory guard SHALL resolve a final-segment symlink identically on every supported interpreter

The safe-directory final-component guard SHALL decide a final-segment symlink by strict real-path resolution and SHALL refuse a resolution loop with the module's structured configuration error on every supported CPython, because non-strict resolution raises an errno-less `RuntimeError` on CPython 3.11 and 3.12 and silently adopts the loop as the field's value on 3.13 and later.

Every strict real-path failure other than a loop SHALL first fall back to non-strict real-path resolution, preserving the established resolution product for a target that does not exist, a target whose parent denies traversal, or a target reached through a regular file. The fallback SHALL NOT itself be treated as proof that the target is absent or a directory: the subsequent final-target classification SHALL distinguish absence, directory, non-directory, and metadata denial without relying on the interpreter-dependent exception swallowing of `Path.exists()` or `Path.is_dir()`. `EACCES` or `EPERM` at that classification boundary SHALL fail closed with the guard's structured `ValueError` safety contract, never leak a raw permission exception and never be accepted as a missing target.

The loop refusal SHALL be recognised by the loop error number obtained from the `errno` module rather than by a hard-coded integer. The guard's separate metadata-lookup failure handler SHALL keep its existing refusal for every error number other than a loop, so that the non-loop geometries already pinned against that handler stay green verbatim.

#### Scenario: A final-segment loop inside the workspace is refused identically on both interpreter arms

- **GIVEN** an evidence directory whose final segment is a symlink loop located inside the workspace root
- **WHEN** the scheduler configuration is constructed on the database-backed arm
- **THEN** it raises the structured configuration error on CPython 3.11 and 3.12 and on 3.13 and later alike
- **AND** the loop is never adopted as the field's value

#### Scenario: The guard reaches the same verdict on both interpreter arms when called directly

- **GIVEN** the same in-workspace final-segment loop
- **WHEN** the safe-directory guard is called directly on CPython 3.11 or 3.12 and on 3.13 or later
- **THEN** both interpreters raise the same structured refusal
- **AND** neither interpreter reaches the containment recheck or the directory check with an adopted loop value

#### Scenario: A dangling final-segment symlink pointing inside the workspace is still accepted

- **GIVEN** a final-segment symlink whose target does not exist and lies inside the workspace root
- **WHEN** the guard runs
- **THEN** it returns without raising, exactly as before this change

#### Scenario: A final-segment target denied by an ancestor is refused identically

- **GIVEN** a final-segment symlink whose resolved target lies under a parent that denies traversal
- **WHEN** the guard classifies the resolved target on any supported CPython
- **THEN** it raises the guard's structured `ValueError` safety refusal
- **AND** it neither leaks a raw `PermissionError` nor accepts the target as absent
- **AND** the refusal is not misattributed to a symlink loop

#### Scenario: A target path reached through a regular file keeps its compatibility verdict

- **GIVEN** a final-segment symlink whose target path is reached through a regular file
- **WHEN** the guard runs
- **THEN** it preserves the pre-change non-loop fallback verdict rather than treating the strict-resolution `ENOTDIR` as a symlink loop

#### Scenario: A dangling final-segment symlink pointing outside the workspace is still refused

- **GIVEN** a final-segment symlink whose target does not exist and lies outside the workspace root
- **WHEN** the guard runs
- **THEN** it keeps today's containment refusal naming the workspace root

#### Scenario: The four pre-existing final-segment verdicts are unchanged

- **GIVEN** in turn a healthy directory symlink, a symlink to a file, a symlink escaping the workspace, and an absent final component
- **WHEN** the guard runs on each
- **THEN** the healthy symlink is accepted, the file target is refused as not a directory, the escaping target is refused as not under the workspace root, and the absent component returns without raising