## ADDED Requirements

### Requirement: Lock-file opens SHALL tolerate a concurrent first creation

The shared safe-filesystem layer SHALL provide one lock-file opener, taking a parent directory descriptor, a final component and a mode. It SHALL:

- attempt an exclusive create (`O_CREAT|O_EXCL`, no-follow);
- on `EEXIST`, open the existing entry without `O_CREAT` and without following a symlink;
- when the entry vanishes between those two opens, retry a small fixed number of times;
- re-raise every other failure unchanged, as the raw `OSError` and not the module's structured error, because the scheduler lease maps errnos itself.

The scheduler lease guard file, the provider destination lock, and the file-journal cycle lock SHALL open their lock file through it. This keeps a concurrent first creation from surfacing as `ENOENT`, which macOS/APFS reports to the loser of a plain `O_CREAT` race, while the callers keep their regular-file check, inode-identity check and error mapping.

#### Scenario: Two concurrent first openers both obtain the lock file

- **GIVEN** a lock file that does not yet exist
- **WHEN** two threads open it through the lock-file opener at the same moment
- **THEN** both obtain a descriptor to the same regular file
- **AND** neither fails with `ENOENT`

#### Scenario: A symlink or directory at the lock name is still refused

- **WHEN** the lock name is a symlink or a directory
- **THEN** the opener fails with the same errno the callers already map, and it never follows the symlink

#### Scenario: A concurrent first acquisition of the journal cycle lock never fails the write

- **GIVEN** a cycle whose lock file does not yet exist
- **WHEN** two journal instances reserve in that cycle for the first time, concurrently
- **THEN** each acquires the cycle lock in turn and the reservation outcome is decided by the journal itself (one admitted, one blocked for overlapping keys)
- **AND** neither reports `FILE_JOURNAL_WRITE_FAILED`
