## MODIFIED Requirements

### Requirement: Scheduler execution extraction preserves pass ordering

Scheduler execution extraction SHALL preserve forcing, cohort grouping,
concurrent submission evidence, and scheduler pass mutation ordering.

#### Scenario: forced production run is scheduled
- **WHEN** a forced production run enters execution after candidate selection
- **THEN** the extracted execution helper preserves the current forcing
  decision, cohort grouping, submission behavior, and evidence payload

#### Scenario: concurrent candidates are submitted
- **WHEN** multiple eligible candidates are submitted in one scheduler pass
- **THEN** concurrent submit evidence, candidate ordering, and mutation fences
  remain the same as before extraction

#### Scenario: published artifact root is absent during planning
- **WHEN** runtime root preflight runs with a missing `published_artifact_root`
  and all other required roots valid
- **THEN** the preflight reports `published_artifact_root.allow_create=true`,
  does not emit `SCHEDULER_ROOT_PUBLISHED_ARTIFACT_ROOT_NOT_FOUND`, and dry-run
  planning remains non-mutating without creating the directory

#### Scenario: temp root is absent at pass start
- **WHEN** a scheduler pass runs its runtime root preflight with a missing
  `temp_root` whose parent is an existing, writable, non-symlink directory,
  the path inside the approved roots, and all other required roots valid
- **THEN** the preflight reports `temp_root.allow_create=true` and does not
  emit `SCHEDULER_ROOT_TEMP_ROOT_NOT_FOUND`
- **AND** the pass creates only the final `temp_root` component without
  following symlinks, with mode 0700, before the preflight verdict is acted on
- **AND** the pass evidence records `root_preflight.checks.temp_root.created=true`
  and the pass is not `preflight_blocked` by `temp_root`

#### Scenario: an absent temp root cannot be created safely
- **WHEN** the missing `temp_root` is outside the approved roots, is itself a
  symlink, or its parent is missing, unwritable, or a symlink
- **THEN** the scheduler reports the existing `SCHEDULER_ROOT_TEMP_ROOT_<REASON>`
  blocker before registry, adapter, active-repository, or submission work
  begins, and creates no directory

#### Scenario: any other required runtime root is absent
- **WHEN** runtime root preflight runs with a missing workspace, object-store,
  runtime, lock, or evidence root
- **THEN** the scheduler reports the existing root blocker before registry,
  adapter, active-repository, or submission work begins
