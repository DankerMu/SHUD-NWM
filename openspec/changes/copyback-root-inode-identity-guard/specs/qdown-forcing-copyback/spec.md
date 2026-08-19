## ADDED Requirements

### Requirement: Publisher copyback same-root branches SHALL be decided by filesystem identity rather than resolved-path string equality

The publisher's run-product copyback branch and its q_down copyback branch SHALL decide "the copyback root and the object-store root are the same storage location" by comparing the `(st_dev, st_ino)` identity of the two directories through the shared no-follow directory-identity probe, rather than by comparing their resolved path strings, so that a same-inode alias cannot be mistaken for a distinct root and mirrored across.

Both branches SHALL keep returning their existing skip result with reason `copyback_root_matches_object_store_root`, and a failure of the identity probe SHALL surface through the publisher's existing copyback-failure error rather than being swallowed.

The publisher's existing evaluation order, in which copyback-root preparation performs its overlap checks before either same-root branch is reached, SHALL be preserved rather than reordered. A typical alias pair — two parallel mount points — is neither string-equal nor string-contained, so it reaches the identity branch; and where a containing alias could be constructed, the overlap refusal that fires first is itself fail-closed, so no bypass results from the retained ordering.

Both operands at both branches are already-resolved paths, which is the precondition the shared probe requires.

#### Scenario: A same-inode alias copyback root takes the publisher run-product skip branch

- **GIVEN** the publisher is asked to copy back run products with an object-store root and a copyback root whose resolved path strings differ
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the same-root branch is evaluated
- **THEN** the call returns its existing skip result with reason `copyback_root_matches_object_store_root`
- **AND** no run products are copied to the aliased root

#### Scenario: A same-inode alias copyback root takes the publisher q_down skip branch

- **GIVEN** the publisher is asked to copy back q_down products with an object-store root and a copyback root whose resolved path strings differ
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the same-root branch is evaluated
- **THEN** the call returns its existing skip result with reason `copyback_root_matches_object_store_root`
- **AND** no q_down products or forcing packages are copied to the aliased root

#### Scenario: Equal configured roots keep their current skip behavior

- **GIVEN** the copyback root and the object-store root are configured as the same path
- **WHEN** either copyback branch is evaluated
- **THEN** the identity comparison holds trivially and the same skip result is returned as before the change
