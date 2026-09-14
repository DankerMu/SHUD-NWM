## MODIFIED Requirements

### Requirement: Canonical mirror cycle trees are deletable by the retention account

The canonical precipitation mirror SHALL be prunable by the node-27 retention
account without root: `canonical/<storage_source>/`, every
`canonical/<storage_source>/<cycle>/` and its `prcp_rate_or_amount/` SHALL be
mode `2775` with a group the retention account belongs to (production: gid
1107, `nwmuser`, shared by node-22 and node-27). The producers set the mode
explicitly on every directory they own — `<cycle>/` before the copied tree is
created, so the tree inherits the parent's group — and the existing tree
obtains group and mode from the documented owner-side sweep. `canonical/`
itself is not swept, so a storage source that has not been swept fails closed:
the first `unlink` is denied and the target is recorded in `failed[]` with
`error_type` `PermissionError` and zero bytes removed. At no level and in no
rollout order SHALL `prcp_rate_or_amount/` be writable by the retention
account while `<cycle>/` is not.

Directory permissions are necessary but no longer sufficient: every canonical
removal holds the copyback batch mutex, whose lock file is mode `0600` and owned
by the copyback root's owner. A retention account that is not that owner cannot
take the mutex, so its canonical removals fail closed with a lock failure of
shape `lock_unsafe` and zero bytes removed, while its raw and
precipitation-cache removals are unaffected. Canonical pruning is effective
only when the retention unit runs as the copyback root's owner.

#### Scenario: a production tick prunes aged canonical cycles

- **WHEN** the retention unit runs in `production_execute` mode after the sweep,
  as the copyback root's owner, with aged canonical cycles under `canonical/gfs`
  and `canonical/IFS`
- **THEN** those cycles appear in `deleted[]` with reason
  `canonical_cycle_aged_out`, `freed_bytes` is greater than zero
- **AND** `failed[]` is empty and the unit ends with `Result=success`

#### Scenario: a retention account that does not own the copyback root

- **WHEN** the retention unit runs in `production_execute` mode as an account
  other than the copyback root's owner, with aged canonical cycles
- **THEN** each aged canonical cycle is recorded in `failed[]` with
  `lock_failure` `lock_unsafe` and no file beneath it is removed
- **AND** no lock file is created when none existed
- **AND** aged raw cycles are still removed and appear in `deleted[]`
- **AND** the summary's `copyback_lock_failures.lock_unsafe` equals the number
  of those canonical entries
- **AND** the process exits `1`

#### Scenario: a freshly mirrored cycle stays deletable

- **WHEN** the merged producer mirrors a new cycle under a swept
  `canonical/<storage_source>/`
- **THEN** `canonical/<storage_source>/<cycle>/` and its
  `prcp_rate_or_amount/` are `2775` with group gid 1107
- **AND** a later tick that finds the cycle aged, run by the copyback root's
  owner, deletes it without a `PermissionError`

#### Scenario: an unswept storage source fails closed

- **WHEN** a storage source directory under `canonical/` still belongs to a
  group the retention account is not in
- **THEN** its aged cycle is recorded in `failed[]` with `error_type`
  `PermissionError` and no file beneath it is removed

#### Scenario: a cycle mirrored before the producer change fails closed until re-swept

- **WHEN** a cycle was mirrored by the pre-change producer under an already
  swept `canonical/<storage_source>/`
- **THEN** its `prcp_rate_or_amount/` is not writable by the retention
  account, the first `unlink` is denied and zero bytes are removed
- **AND** the documented re-sweep makes it deletable
