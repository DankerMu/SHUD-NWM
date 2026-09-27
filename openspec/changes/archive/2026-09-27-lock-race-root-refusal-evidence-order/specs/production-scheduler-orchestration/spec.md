## ADDED Requirements

### Requirement: The scheduler lock parent SHALL refuse a looping or non-directory workspace root with its structured reason on every supported interpreter

When the configured workspace root is, after resolution, a symlink loop or an existing non-directory entry, opening the scheduler lock parent SHALL raise the structured `unsafe_lock_parent_directory` refusal on CPython 3.11 and on 3.13+ alike. It SHALL never raise an errno-less `RuntimeError` or a bare `FileExistsError`. As a result, lease acquisition reports `acquired=False` with that reason, and evidence directory preparation reports `unsafe_evidence_directory`. A workspace root that is a symlink to a real directory SHALL remain accepted.

#### Scenario: A looping workspace root is refused with the structured reason

- **GIVEN** a workspace root that is a two-link symlink loop, or a self loop
- **WHEN** the scheduler lease is acquired
- **THEN** it returns `acquired=False` with reason `unsafe_lock_parent_directory` and does not raise

#### Scenario: A symlinked workspace root to a real directory still works

- **WHEN** the workspace root is a symlink to an existing directory
- **THEN** the lease is acquired as before

### Requirement: Object-store retention SHALL refuse an unexpandable root with a typed skip reason

When a configured root cannot be expanded because its leading `~user` names a user with no determinable home directory, object-store retention SHALL record a typed skip instead of raising an errno-less `RuntimeError`:

- an unexpandable primary root SHALL be skipped with `primary_root_unexpandable`;
- an unexpandable additional root SHALL be skipped with `extra_root_unexpandable`, and SHALL drop only itself, so the valid primary root's plan is unchanged;
- an unexpandable published protected root SHALL be recorded as `published_root_unexpandable`, and the pass SHALL plan no deletion at all, because deleting without the protection root is not allowed.

#### Scenario: A bad additional root does not stop the primary sweep

- **GIVEN** a valid primary root and an additional root of the form `~nosuchuser/...`
- **WHEN** retention runs
- **THEN** the additional root is skipped with `extra_root_unexpandable`
- **AND** the primary root's plan equals that of a run without the additional root

#### Scenario: An unexpandable published root plans nothing

- **WHEN** the published protected root is `~nosuchuser/...`
- **THEN** the result carries a `published_root_unexpandable` skip, no target is planned, and a non-dry run deletes nothing

#### Scenario: The cleanup CLI reports instead of crashing

- **WHEN** `cleanup` runs with `OBJECT_STORE_ROOT=~nosuchuser/...`
- **THEN** it prints the structured result carrying `primary_root_unexpandable`, exits 0, and no traceback is shown

### Requirement: Bounded pass evidence SHALL shed the candidate lists before the breaker-released source-cycle projection

When the bounded fallback evidence still exceeds the byte budget, its shedding order SHALL be:

1. model discovery
2. the three candidate summary lists
3. the `source_cycles` breaker-released projection
4. the restart reconcile block
5. the model-run failure projection

Both shedding tiers use this order. The capped projection is the only evidence that no other cycle was released by the breaker, so it SHALL survive whenever emptying the candidate lists is enough.

#### Scenario: Clearing the candidate lists preserves the projection

- **GIVEN** a bounded product with a non-empty breaker-released projection and large candidate lists
- **WHEN** clearing the candidate lists is enough to fit the budget
- **THEN** `limit.source_cycles.status` is `summarized`, the projection rows are kept, and `limit.candidate_lists` is `dropped`
- **AND** the operator-action listing reports `size_fallback_source_cycles_summarized` and lists the released models

### Requirement: A heartbeat-refreshed reservation under evidence retention SHALL never yield a false clean operator-action answer

When the evidence retention policy runs on an evidence root that holds a pass's reservation and its terminal evidence file, the operator-action listing SHALL never exit `0` merely because retention removed the terminal file first. This holds even when the reservation's modification time is newer than that terminal file's, as happens when a heartbeat touch lands between the terminal write and the heartbeat stop.

#### Scenario: An inverted mtime order stays fail-closed

- **GIVEN** a reservation whose mtime is newer than its own terminal file's, a byte budget that forces one oldest-first deletion, and no newer evaluating, scope-complete pass on the root
- **WHEN** retention deletes the terminal file first and the listing then runs
- **THEN** the listing exits `3`, not `0`

#### Scenario: A crashed pass under size pressure does not fall back to a clean older pass

- **GIVEN** a crashed pass's reservation without a terminal file, and an older evaluating terminal file
- **WHEN** size retention deletes the older terminal file and the listing then runs
- **THEN** the listing exits `3`, not `0`
