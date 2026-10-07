# basin-retirement Specification

## Purpose
The node-27 tool that retires a basin version after its models were removed from the scheduler on node-22:
it adds the basin to the autopipe exclusion list, marks the basin version's displayable runs superseded,
deactivates its model rows and verifies that an autopipe round did not revert any of it, each step gated on
the receipt of the one before.

## Requirements

### Requirement: Basin retirement on node-27 runs in a fixed, receipt-gated order

The node-27 basin retirement tool SHALL run the steps `exclude`, `supersede`, `deactivate`, `verify` in that
order for one basin version per run, SHALL write one receipt per completed step into a directory of that
basin version under the succession, and MUST refuse a step whose preceding step has no receipt. It MUST
refuse to run any step unless the node-22 succession of the same id has finished with kind `remove_basin`,
the plan removed at least one model of the basin version, and the canonical scheduler manifest holds no row
of the basin: neither of the basin version nor of any other version of the same basin.

#### Scenario: Full retirement

- **WHEN** the tool is applied for a basin version whose node-22 removal has finished
- **THEN** the four step receipts are written in order
- **AND** the basin key is in `AUTOPIPE_EXCLUDE_BASINS`, the runs of the basin version in `succeeded`,
  `parsed` or `published` are `superseded`, and no `core.model_instance` row of it is active

#### Scenario: The node-22 removal has not finished

- **WHEN** the succession has no finish receipt of kind `remove_basin`
- **THEN** the tool refuses before any file or database write

#### Scenario: Another version of the basin is still scheduled

- **WHEN** the manifest holds a row of the same basin under another basin version
- **THEN** the tool refuses before any file or database write

#### Scenario: Two basins of one succession

- **WHEN** the tool is run for a second basin version of the same succession
- **THEN** its steps run and its receipts and backups are separate from those of the first

#### Scenario: A step before its predecessor

- **WHEN** `supersede`, `deactivate` or `verify` is reached without the receipt of the step before it
- **THEN** the step is refused and nothing is changed

### Requirement: Model rows are deactivated only after an autopipe round that honours the exclusion

After ensuring the basin key is in the exclusion list, the tool SHALL wait for an autopipe service run that
started after the env file edit (or, when the key was already present, after the step began), ran to its
own exit past its preflight, and was not merely skipped while another round held the autopipe lock, before it changes a
database row; and SHALL wait for another such run after deactivation before it verifies. It MUST NOT start
or stop any unit.

#### Scenario: A round in flight at the edit

- **WHEN** an autopipe run started before the env file was edited
- **THEN** that run does not satisfy the wait

#### Scenario: A round that proves nothing

- **WHEN** a run was blocked at its preflight, was ended by a signal, or ended while the autopipe lock was
  held by another round
- **THEN** that run does not satisfy the wait

#### Scenario: Rounds back to back

- **WHEN** the unit is running on every poll and a round that started after the reference is followed by a
  round with a later start
- **THEN** the earlier of the two satisfies the wait

#### Scenario: A round in which another basin failed

- **WHEN** a run started after the reference and exited with the status autopipe uses for failed runs
- **THEN** that run satisfies the wait

#### Scenario: No round completes in time

- **WHEN** no qualifying run ends within the wait limit
- **THEN** the step fails, no database row was changed by it, and a rerun waits again without rewriting the
  env file

#### Scenario: Autopipe reverted the retirement

- **WHEN** `verify` finds an active model row, a run of the basin version back in a candidate status, or the
  key gone from the exclusion list
- **THEN** the step fails naming what was reverted

### Requirement: The exclusion edit is minimal, backed up and refuses an unexpected env file

The tool SHALL change only the single `AUTOPIPE_EXCLUDE_BASINS` assignment line of the env file, after writing
a backup of the file, by atomic replacement, keeping mode 0600. It MUST refuse without writing when the env
file is not a regular file owned by the user with mode 0600, is a symlink, does not contain exactly one plain
unquoted assignment of that variable with nothing after the value, or contains any other assignment of it.
The basin key SHALL be derived and compared with the autopipeline's own normalisation. The tool MUST refuse
to run when the database it would write to is not the one named in that env file, and when another instance
is running.

#### Scenario: Key appended

- **WHEN** the key is not in the list
- **THEN** the backup exists, the line holds the previous entries followed by the key, and every other byte
  of the file is unchanged

#### Scenario: Key already present

- **WHEN** the list already holds the key, in any spelling the normalisation equates
- **THEN** the file is not written

#### Scenario: Unexpected env file

- **WHEN** the env file is a symlink, has another mode, or has zero, two, a quoted, a commented or an
  exported assignment
- **THEN** `exclude` fails and the file is unchanged

#### Scenario: Another database

- **WHEN** `DATABASE_URL` of the process differs from the one in the env file
- **THEN** the tool refuses before anything is read from the database or written

### Requirement: Runs are superseded in one transaction with a backup, and model rows through the lifecycle operation

The tool SHALL back up the rows of `hydro.hydro_run` of the basin version in `succeeded`, `parsed` or
`published` to a file and set their status to `superseded` in one transaction, changing no other column, and
MUST roll back when the updated rows are not exactly the backed-up rows. Once the commit has been attempted
the backup MUST be kept, whatever happens next. It SHALL deactivate every active
`core.model_instance` row of the basin version, selected by exact `basin_version_id`, through the model
lifecycle operation with an explicit policy decision, after a preflight of all rows found no blocker, and
MUST treat any returned status other than a completed transition as a failure. It MUST NOT write
`core.basin`, `core.basin_version` or any Basins directory.

#### Scenario: Updated rows differ from the backup

- **WHEN** the update affects rows other than exactly those the backup holds
- **THEN** the transaction is rolled back and no receipt is written

#### Scenario: The commit outcome is not known

- **WHEN** the commit call raises or the run is interrupted after it
- **THEN** the backup is kept and no receipt is written

#### Scenario: The backup is already there

- **WHEN** a rerun finds the run backup of an earlier attempt
- **THEN** with no run left in the three statuses it writes the receipt from the backup and changes nothing
- **AND** with runs left in them it fails without changing anything

#### Scenario: A preflight blocker

- **WHEN** the preflight of any active row reports a blocker
- **THEN** no row is deactivated

#### Scenario: The lifecycle operation returns a failure

- **WHEN** the operation on a row returns a status that is not a completed transition
- **THEN** the step fails, the failure receipt names the rows done, and a rerun acts only on rows still active

### Requirement: A retirement dry-run commits nothing

Without `--apply` the tool SHALL change no file, write no receipt, commit no write transaction, query units
read-only and not wait for an autopipe round. Its supersede step SHALL execute the same statements as an
apply and roll them back.

#### Scenario: Dry-run

- **WHEN** the tool runs without `--apply`
- **THEN** the env file and the succession directory are unchanged and no write was committed
- **AND** the report gives the per-status run counts and the preflight result of every active model row
