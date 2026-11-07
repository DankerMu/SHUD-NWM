## ADDED Requirements

### Requirement: An opt-in forcing lane SHALL retire aged forcing cycle trees under the copyback mutex

The runner SHALL provide a `forcing` lane whose targets are the real (non-symlink) directories
`<object-store-root>/forcing/<segment>/<YYYYMMDDHH>`, where `<segment>` is the lower-case
object-store segment of each configured source (`gfs`, `ifs`), the spelling the forcing producer
writes. Only directories are listed; a symlink or a plain file under a source directory SHALL be
neither listed nor removed. A directory whose name is not a 10-digit UTC cycle SHALL be kept as
`unparseable_cycle_name`; a source that does not map SHALL be kept as `source_unmappable`. The lane
SHALL use its own cutoff, `reference_time - NODE27_RAW_RETENTION_FORCING_DAYS` (default 30 days), on
the same display-watermark anchor as the other lanes, and SHALL plan only cycles strictly older than
that cutoff. Every forcing target SHALL be removed while holding the object-store copyback batch
mutex for that one tree, drawing on the same pass-level wait budget as the canonical lane; a lock
failure SHALL leave the tree in place, SHALL be recorded in `failed[]` with its `lock_failure` kind,
and SHALL be counted in `copyback_lock_failures`. The summary SHALL always carry `forcing_root`,
`forcing_retention_days` and `forcing_cutoff`.

#### Scenario: an aged forcing cycle is removed under the mutex

- **GIVEN** `NODE27_RAW_RETENTION_LANES=forcing`, `NODE27_RAW_RETENTION_FORCING_DAYS=30` and a
  reference time of 2026-11-07T00:00Z
- **WHEN** directories `forcing/gfs/2026100612`, `forcing/ifs/2026100612`, `forcing/gfs/2026101000`
  and `forcing/gfs/tmp`, a symlink `forcing/ifs/2026100100` and a file `forcing/ifs/README` exist
- **THEN** `forcing/gfs/2026100612` and `forcing/ifs/2026100612` are planned and deleted, each with
  the copyback batch mutex held during its removal
- **AND** `forcing/gfs/2026101000` is kept as `within_retention_window` and `forcing/gfs/tmp` as
  `unparseable_cycle_name`
- **AND** the symlink and the file are still on disk and appear in no summary list

#### Scenario: the copyback mutex is held elsewhere

- **WHEN** another process holds the copyback batch lock past the pass wait budget and an aged
  forcing cycle exists
- **THEN** that cycle stays on disk, appears in `failed[]` with `lock_failure` `lock_timeout`, and is
  counted in `copyback_lock_failures`
- **AND** the process exits non-zero

### Requirement: The forcing retention period SHALL NOT fall below the display and ingest floor

The runner SHALL read `NODE27_RAW_RETENTION_FORCING_DAYS` strictly: unset or blank means 30, and any
other value SHALL be a string of decimal digits. The runner SHALL block at preflight, delete nothing,
and exit non-zero when the value is not such a string, is not positive, or is below
`max(NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS + 1, 14)`, where 14 is the production autopipe cycle floor
(`AUTOPIPE_MAX_CYCLE_AGE_DAYS`) whose ingest still reads the forcing handoff. The blocker SHALL name
the field `forcing_retention_days` and carry the value and the floor. The check SHALL apply whether
or not the `forcing` lane is selected.

#### Scenario: a forcing period below the floor

- **WHEN** `NODE27_RAW_RETENTION_FORCING_DAYS` is `13`, `0` or `abc` and the selected lanes are
  `raw,precip-cache`
- **THEN** the run is blocked at preflight with a `forcing_retention_days` blocker reporting the value
  and floor 14
- **AND** no file under any lane is removed

## MODIFIED Requirements

### Requirement: A retention run SHALL prune only the lanes its operator selected

The runner SHALL read `NODE27_RAW_RETENTION_LANES` from the environment as a
comma-separated list of lane names from `raw`, `canonical`, `precip-cache` and
`forcing`, trimming surrounding whitespace. When the variable is unset the run
SHALL select exactly `raw`, `canonical` and `precip-cache` and behave exactly as
before the variable existed: the `forcing` lane SHALL NOT be selected, probed,
listed or locked, and SHALL contribute no `skipped[]` entry. When it is set but
holds no lane name, or holds any name outside the four, the run SHALL be blocked
at preflight with a `lanes` blocker and SHALL delete nothing. A lane that is not
selected SHALL contribute exactly one `skipped[]` entry with `key` equal to the
lane name and `reason` `lane_not_selected`, except that the `forcing` lane SHALL
contribute no entry when the selection is exactly the legacy three (unset or
written out), so that selection stays the pre-forcing run byte for byte; its
root SHALL NOT be probed, listed or deleted from, and no copyback lock SHALL be
taken or created for it. The summary SHALL record the selected lanes as a sorted
list.

#### Scenario: unset selects every lane

- **WHEN** the variable is unset and aged targets exist in all four lanes
- **THEN** aged raw, canonical and precip-cache targets are all planned
- **AND** no forcing target is planned, no `forcing` key appears in `skipped[]`,
  and no `lane_not_selected` entry appears

#### Scenario: the nwm unit excludes the canonical lane

- **WHEN** the variable is `raw,precip-cache`, aged canonical cycles exist and
  the copyback lock file is not openable by the runner account
- **THEN** aged raw and precip-cache targets are deleted
- **AND** `skipped[]` holds `{"key": "canonical", "reason": "lane_not_selected"}`
  and `{"key": "forcing", "reason": "lane_not_selected"}`
- **AND** `copyback_lock_failures` counts are all zero, `failed[]` is empty and
  the process exits `0`

#### Scenario: the canonical unit prunes only canonical

- **WHEN** the variable is `canonical` (or `canonical,forcing` for forcing too) and aged targets exist in all four lanes
- **THEN** only aged canonical cycles (and, with `canonical,forcing`, aged forcing cycles) are planned
- **AND** `skipped[]` holds `lane_not_selected` entries for `raw` and
  `precip-cache`, and with `canonical` alone also for `forcing`

#### Scenario: an unknown or empty selection fails closed

- **WHEN** the variable is `raw,canon` or a value that trims to no lane name
- **THEN** the run is blocked at preflight with a `lanes` blocker
- **AND** no file under any lane is removed

#### Scenario: an unselected lane with an unusable root is not probed

- **WHEN** the variable is `canonical` and the raw lane root does not exist
- **THEN** no `raw_root_unsafe` entry appears
- **AND** the raw lane is represented only by its `lane_not_selected` entry

### Requirement: On node-27 the canonical lane SHALL run as the copyback root's owner and every raw-retention unit SHALL alert on failure

node-27 SHALL run the canonical and forcing lanes in the system unit
`nhms-node27-canonical-retention.service` with `User=frd_muziyao` (the copyback
root's owner, uid 1103) and `NODE27_RAW_RETENTION_LANES=canonical,forcing`,
driven by `nhms-node27-canonical-retention.timer`, and SHALL run the raw and
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
- **THEN** the canonical unit's summary lists the aged canonical and forcing
  cycles in `deleted[]`, its `copyback_lock_failures` counts are all zero and
  its unit ends `Result=success`
- **AND** the user unit's summary lists aged raw and precip-cache targets in
  `deleted[]`, `failed[]` is empty and its unit ends `Result=success`
- **AND** the lock file is still `-rw-------` owned by `frd_muziyao`

#### Scenario: a failing tick reaches the operator

- **WHEN** either unit ends `Result=failed`
- **THEN** its `OnFailure=` alert unit runs the alert handler for that unit
- **AND** for the system unit the handler reads the system journal
  (`NHMS_UNIT_FAILURE_JOURNAL_SCOPE=system`), while an unset or other value
  keeps reading the user journal
