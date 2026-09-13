# node27-raw-retention Specification

## Purpose
TBD - created by archiving change fix-raw-retention-lane-root-locality. Update Purpose after archive.
## Requirements
### Requirement: A lane's traversal failure retires only that lane or source

The node-27 retention runner (`scripts/node27_raw_retention.py`) SHALL treat an
`OSError` raised while probing or listing a lane root (`raw/`, `canonical/`,
`<cache>/precip/`) or a per-source root beneath it (`raw/<source>`,
`canonical/<storage_source>`, `<cache>/precip/<storage_source>`) as a skip of
that lane or source only. The run SHALL continue to collect and delete targets
of every other lane and source on the same tick, SHALL write its summary to the
configured summary path, and SHALL return exit code `0` when no deletion failed.
A listing that raised SHALL NOT be reported as an empty directory.

#### Scenario: canonical root not traversable by the runner account

- **WHEN** `<object-store>/canonical` exists but the runner account lacks `x`
  on it, and aged targets exist in the raw and precip-cache lanes
- **THEN** the raw and precip-cache targets are deleted
- **AND** `skipped[]` carries one entry per configured source with reason
  `canonical_source_unsafe` and detail `path_unavailable`
- **AND** the summary file is written and `exit_code == 0`

#### Scenario: raw root readable but not traversable

- **WHEN** `<object-store>/raw` is listable (`r`) but not traversable (`x`)
- **THEN** `skipped[]` carries `raw_root_unsafe` / `path_unavailable` for the
  raw lane and no raw target is deleted
- **AND** the canonical and precip-cache lanes still delete their aged targets
  on the same tick

#### Scenario: one source root unreadable inside a traversable lane

- **WHEN** `<object-store>/canonical` is traversable and
  `<object-store>/canonical/IFS` is listable but not traversable
- **THEN** `skipped[]` carries `canonical_source_unsafe` / `path_unavailable`
  for `canonical/IFS` only
- **AND** the aged `canonical/gfs`, raw and precip-cache targets are deleted

#### Scenario: one raw source directory unreadable inside a traversable raw lane

- **WHEN** `<object-store>/raw` is traversable and `<object-store>/raw/gfs` is
  listable but not traversable
- **THEN** `skipped[]` carries `raw_source_unsafe` / `path_unavailable` with
  key `raw/gfs` and no raw target is deleted
- **AND** the aged canonical and precip-cache targets are deleted

#### Scenario: one precip-cache source directory unreadable inside a traversable cache lane

- **WHEN** `<cache>/precip` is traversable and `<cache>/precip/IFS` is mode `0o444`
- **THEN** `skipped[]` carries one entry with key `precip-cache/IFS`, reason
  `precip_cache_source_unsafe`, detail `path_unavailable` and `error_type`
  `PermissionError`
- **AND** aged cycles under `<cache>/precip/gfs` are still deleted

### Requirement: Unavailable-path skip entries carry the errno

Skip entries with detail `path_unavailable` SHALL carry `error` (the `OSError`
text) and `error_type` (the exception class name) in addition to `key`,
`reason`, `path` and `detail`. This applies to lane-root entries
(`<lane>_root_unsafe`) and per-source entries (`<lane>_source_unsafe`,
`raw_source_unsafe`). The `(reason, detail)` pair of existing entries MUST NOT
change.

#### Scenario: an unreadable object-store ancestor

- **WHEN** `<object-store>` itself is not traversable
- **THEN** both `raw_root_unsafe` and `canonical_root_unsafe` skip entries have
  detail `path_unavailable`, a non-empty `error` and `error_type` equal to
  `PermissionError`

#### Scenario: a stale lane-root handle

- **WHEN** resolving `<object-store>/raw` raises `ESTALE`
- **THEN** the raw lane skip is `raw_root_unsafe` / `path_unavailable` with
  `error` naming the stale handle and `error_type` equal to `OSError`

### Requirement: The documented operator check reports every unsafe skip

The documented operator check SHALL exit non-zero when `skipped[]` contains
any entry whose reason ends with `_unsafe` (the `jq` program in
`infra/env/node27-raw-retention.example`), and SHALL exit zero on a summary
the check otherwise accepts that carries no such entry. What the check
accepts in `failed[]` is not fixed by this requirement: this change leaves
the shipped clause as it found it, and #2100 owns that dimension.

#### Scenario: a per-source skip is not silently green

- **WHEN** the documented `jq` program runs on the summary of the
  canonical-root-untraversable tick
- **THEN** it exits `1`
- **AND** on a healthy production-shaped summary it exits `0`

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

