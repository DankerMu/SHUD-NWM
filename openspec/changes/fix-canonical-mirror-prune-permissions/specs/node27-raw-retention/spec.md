# Spec Delta: node27-raw-retention

## ADDED Requirements

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

#### Scenario: a production tick prunes aged canonical cycles

- **WHEN** the retention unit runs in `production_execute` mode after the sweep
  with aged canonical cycles under `canonical/gfs` and `canonical/IFS`
- **THEN** those cycles appear in `deleted[]` with reason
  `canonical_cycle_aged_out`, `freed_bytes` is greater than zero
- **AND** `failed[]` is empty and the unit ends with `Result=success`

#### Scenario: a freshly mirrored cycle stays deletable

- **WHEN** the merged producer mirrors a new cycle under a swept
  `canonical/<storage_source>/`
- **THEN** `canonical/<storage_source>/<cycle>/` and its
  `prcp_rate_or_amount/` are `2775` with group gid 1107
- **AND** a later tick that finds the cycle aged deletes it without a
  `PermissionError`

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

### Requirement: A canonical PermissionError is reported red

The documented operator check for the retention summary SHALL exit non-zero
when `failed[]` contains any entry, `PermissionError` included; the
"PermissionError is the expected steady state" reading is withdrawn.

#### Scenario: the documented check on a summary with a PermissionError

- **WHEN** the documented `jq` program runs on a fresh `production_execute`
  summary whose `failed[]` holds one canonical `PermissionError` entry
- **THEN** it exits `1`
- **AND** on a fresh `production_execute` summary with empty `failed[]` and no
  `*_unsafe` skip it exits `0`
