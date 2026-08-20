## ADDED Requirements

### Requirement: The forcing-copyback backfill same-root guard SHALL be decided by filesystem identity rather than resolved-path string equality

The backfill's same-root rejection SHALL decide sameness by comparing the `(st_dev, st_ino)` identity of the copyback root and the object-store root through the shared no-follow directory-identity probe, rather than by comparing their resolved path strings, so that a same-inode alias cannot be mistaken for a distinct root and copied across.

The existing failure surface SHALL be preserved exactly: the same `COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT` error with `details.reason` `copyback_root_matches_object_store_root`, and the existing overlap check kept as resolved-path string comparison.

#### Scenario: A same-inode alias copyback root is rejected by the backfill

- **GIVEN** the backfill is configured with an object-store root and a copyback root whose resolved path strings differ
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the copyback-root boundary is validated
- **THEN** the backfill fails closed with `COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT` and `details.reason` `copyback_root_matches_object_store_root`
- **AND** no objects are copied

### Requirement: Each backfill same-root call site SHALL keep its current error posture when the identity probe fails

The backfill's same-root rejection SHALL receive already-computed root identities rather than probing the filesystem itself, so that every call site owns its probe and its failure posture explicitly.

The object-store side identity SHALL be computed once, where the object-store root is already verified, and carried alongside that verified path; a failure of that probe SHALL surface as the existing object-store-root unsafety error, never as a copyback-root diagnosis. Each call site is then responsible only for the copyback side.

The apply path SHALL wrap its probe and raise `COPYBACK_ROOT_UNSAFE`, matching its strict sibling, and SHALL NOT let a filesystem error escape into the CLI's generic handler where it would be reported as a generic backfill failure instead of a copyback-root diagnosis. The raw-path short-circuit, which fires before resolution when both configured strings are equal, SHALL NOT probe at all and SHALL compare the object-store identity with itself. The existing-root pre-check SHALL keep returning silently when its probe fails, exactly as it returns silently today when the copyback root is absent or unreadable. The dry-run path SHALL keep raising `COPYBACK_ROOT_UNSAFE`.

#### Scenario: Absent or unreadable copyback roots keep their current lenient handling

- **GIVEN** the backfill's existing-root pre-check runs against a copyback root that does not yet exist
- **WHEN** the identity comparison would need a probe of that root
- **THEN** the pre-check returns without raising, exactly as before the change
- **AND** a distinct, existing copyback root still passes the boundary check and proceeds

#### Scenario: An apply-path probe failure is diagnosed as an unsafe copyback root

- **GIVEN** the backfill runs in apply mode and the identity probe of the prepared copyback root fails with a filesystem error
- **WHEN** the same-root boundary is evaluated
- **THEN** the backfill raises `COPYBACK_ROOT_UNSAFE`
- **AND** it does not surface as the CLI's generic backfill failure code

#### Scenario: An object-store-root probe failure is diagnosed on the object-store side

- **GIVEN** the identity probe of the object-store root fails with a filesystem error
- **WHEN** the backfill verifies the object-store root
- **THEN** it raises the existing object-store-root unsafety error
- **AND** the reported details name the object-store root, not the copyback root
