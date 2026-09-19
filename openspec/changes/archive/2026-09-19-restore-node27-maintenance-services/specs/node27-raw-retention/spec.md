## ADDED Requirements

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
