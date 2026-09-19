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

### Requirement: A retention run SHALL prune only the lanes its operator selected

The runner SHALL read `NODE27_RAW_RETENTION_LANES` from the environment as a
comma-separated list of lane names from `raw`, `canonical` and `precip-cache`,
trimming surrounding whitespace. When the variable is unset the run SHALL
select all three lanes and behave exactly as before the variable existed. When
it is set but holds no lane name, or holds any name outside the three, the run
SHALL be blocked at preflight with a `lanes` blocker and SHALL delete nothing.
A lane that is not selected SHALL contribute exactly one `skipped[]` entry with
`key` equal to the lane name and `reason` `lane_not_selected`; its root SHALL
NOT be probed, listed or deleted from, and no copyback lock SHALL be taken or
created for it. The summary SHALL record the selected lanes as a sorted list.

#### Scenario: unset selects every lane

- **WHEN** the variable is unset and aged targets exist in all three lanes
- **THEN** aged raw, canonical and precip-cache targets are all planned
- **AND** no `lane_not_selected` entry appears

#### Scenario: the nwm unit excludes the canonical lane

- **WHEN** the variable is `raw,precip-cache`, aged canonical cycles exist and
  the copyback lock file is not openable by the runner account
- **THEN** aged raw and precip-cache targets are deleted
- **AND** `skipped[]` holds `{"key": "canonical", "reason": "lane_not_selected"}`
- **AND** `copyback_lock_failures` counts are all zero, `failed[]` is empty and
  the process exits `0`

#### Scenario: the canonical unit prunes only canonical

- **WHEN** the variable is `canonical` and aged targets exist in all three lanes
- **THEN** only aged canonical cycles are planned
- **AND** `skipped[]` holds `lane_not_selected` entries for `raw` and
  `precip-cache`

#### Scenario: an unknown or empty selection fails closed

- **WHEN** the variable is `raw,canon` or a value that trims to no lane name
- **THEN** the run is blocked at preflight with a `lanes` blocker
- **AND** no file under any lane is removed

#### Scenario: an unselected lane with an unusable root is not probed

- **WHEN** the variable is `canonical` and the raw lane root does not exist
- **THEN** no `raw_root_unsafe` entry appears
- **AND** the raw lane is represented only by its `lane_not_selected` entry

### Requirement: On node-27 the canonical lane SHALL run as the copyback root's owner and every raw-retention unit SHALL alert on failure

node-27 SHALL run the canonical lane in the system unit
`nhms-node27-canonical-retention.service` with `User=frd_muziyao` (the copyback
root's owner, uid 1103) and `NODE27_RAW_RETENTION_LANES=canonical`, driven by
`nhms-node27-canonical-retention.timer`, and SHALL run the raw and
precip-cache lanes in the `nwm` user unit `nhms-node27-raw-retention.service`
with `NODE27_RAW_RETENTION_LANES=raw,precip-cache`. The copyback lock file's
mode (`0600`) and owner SHALL NOT change. The user unit SHALL declare
`OnFailure=nhms-node27-unit-failure-alert@%n.service`; the system unit SHALL
declare `OnFailure=nhms-node27-system-unit-failure-alert@%n.service`, a
system-scope template that runs the same alert handler as `nwm` with read
access to the system journal and quotes that unit's system-journal lines.

#### Scenario: a production tick of each unit

- **WHEN** both units run a `production_execute` tick with aged targets in
  every lane
- **THEN** the canonical unit's summary lists the aged canonical cycles in
  `deleted[]`, its `copyback_lock_failures` counts are all zero and its unit
  ends `Result=success`
- **AND** the user unit's summary lists aged raw and precip-cache targets in
  `deleted[]`, `failed[]` is empty and its unit ends `Result=success`
- **AND** the lock file is still `-rw-------` owned by `frd_muziyao`

#### Scenario: a failing tick reaches the operator

- **WHEN** either unit ends `Result=failed`
- **THEN** its `OnFailure=` alert unit runs the alert handler for that unit
- **AND** for the system unit the handler reads the system journal
  (`NHMS_UNIT_FAILURE_JOURNAL_SCOPE=system`), while an unset or other value
  keeps reading the user journal

