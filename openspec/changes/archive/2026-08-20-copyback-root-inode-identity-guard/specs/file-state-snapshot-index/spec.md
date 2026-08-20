## ADDED Requirements

### Requirement: Copyback root sameness SHALL be decided by filesystem identity rather than resolved-path string equality

The state-index copyback replay tool and the natural run-tree copyback path SHALL decide "reference root and destination root are the same storage location" by comparing the `(st_dev, st_ino)` identity of the two directories, obtained through a single shared no-follow descriptor probe, instead of comparing their resolved path strings.

Both sites SHALL keep their existing structured outcome for the same-root case — the replay tool raises `roots_identical` and the run-tree path returns its `copyback_root_matches_object_store_root` skip — and SHALL NOT introduce a new error code or change the payload fields.

Overlap (parent/child containment) detection SHALL remain resolved-path string comparison, because inode identity cannot express containment; consequently an aliased parent/child overlap remains undetectable, which is an accepted limit of this change.

The identity comparison SHALL be evaluated before the overlap comparison at both sites, preserving today's ordering, and a failure of the identity probe SHALL surface through each site's existing root-unavailability error rather than being swallowed or degraded into a permissive pass.

#### Scenario: Same-inode alias roots are refused by the replay tool instead of deadlocking

- **GIVEN** the replay tool is invoked with a reference root and a destination root whose resolved path strings differ
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the root-conflict guard runs
- **THEN** the tool fails closed with the structured reason `roots_identical` and a non-zero exit
- **AND** no state index is written at the destination and no partial receipt is produced
- **AND** the call returns within a bounded timeout rather than blocking on the provider destination lock

#### Scenario: Same-inode alias roots take the run-tree copyback skip branch

- **GIVEN** `copyback_run_trees` is called with an object-store root and a copyback root whose resolved path strings differ, and with the state-index object key among its extra object keys
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the same-root guard runs
- **THEN** the call returns the existing skip result with reason `copyback_root_matches_object_store_root`
- **AND** `merge_state_snapshot_index_copyback` is never reached
- **AND** the call returns within a bounded timeout rather than blocking on the provider destination lock

#### Scenario: Genuinely distinct roots and symlink aliases keep their current behavior

- **GIVEN** a reference root and a destination root that are two distinct directories with distinct filesystem identities
- **WHEN** the root-conflict guard runs
- **THEN** the copyback proceeds exactly as before the change, provided every ancestor directory of both roots is readable
- **AND** when the destination root is instead a symlink alias of the reference root, resolution still collapses it and it is still refused as the same root
- **AND** when one root is a strict subdirectory of the other, the existing `roots_overlap` refusal still fires

### Requirement: A single shared no-follow directory-identity probe SHALL be the only source of root identity for the guards this change touches

A helper `directory_identity_no_follow` SHALL exist in the shared safe-filesystem module, returning the `(st_dev, st_ino)` pair of an existing directory opened through the module's existing per-component `O_NOFOLLOW` descriptor walk followed by `os.fstat`, and each copyback same-root guard changed by this change SHALL obtain root identity only through that helper.

The guards in question are exactly: the state-index copyback replay root-conflict guard, the run-tree copyback same-root branch, the tile-publisher run-product and q_down copyback same-root branches, and the forcing-copyback backfill same-root rejection. No claim is made about guards outside this enumeration.

The helper SHALL NOT introduce any path-following stat call, and SHALL close the descriptor it opens on every exit path. Because the descriptor walk rejects every symlink component including the final one, the helper SHALL be called only on already-resolved paths.

The identity claim carried by this helper is limited to aliases that share a filesystem superblock; aliases that present the same object under different `st_dev` values are outside what this comparison can detect.

#### Scenario: The probe reports identity, not the input string

- **GIVEN** one directory addressed through two different input strings that expand to the same resolved path
- **WHEN** the probe is called on each string
- **THEN** both calls return the same `(st_dev, st_ino)` pair
- **AND** the probe called on a second, genuinely distinct directory returns a different pair

#### Scenario: The probe refuses symlink components instead of following them

- **GIVEN** a path whose final component is a symlink pointing at a directory
- **WHEN** the probe is called on it
- **THEN** the call raises rather than returning the identity of the link target
