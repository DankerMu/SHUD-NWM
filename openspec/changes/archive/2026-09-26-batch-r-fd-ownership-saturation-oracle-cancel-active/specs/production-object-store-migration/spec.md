## ADDED Requirements

### Requirement: Runtime staging prefix open releases its descriptor on every rejection

Every owner of the runtime staging prefix directory open (`_open_runtime_prefix_dir`) SHALL close the directory descriptor exactly once on every exception raised after `os.open()` succeeds. This covers a post-open `fstat` failure, a containment or no-follow rejection, and a not-a-directory rejection. The owner SHALL then raise the same `ProductionObjectStoreValidationError` code and message as before. A failure to close SHALL NOT replace that error. On success, the owner SHALL return a live descriptor that is owned by the caller.

#### Scenario: Containment rejection after open closes the descriptor

- **WHEN** the directory opens but `stat_no_follow()` rejects it because it lies outside the containment root
- **THEN** the owner raises `PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE`, as before
- **AND** the descriptor that was opened is closed

#### Scenario: Not-a-directory rejection after open closes the descriptor

- **WHEN** the opened descriptor is not a directory
- **THEN** the owner raises `Runtime staging prefix is not a directory: <path>` with the existing code
- **AND** the descriptor is closed exactly once

#### Scenario: Success returns a live descriptor

- **WHEN** every check passes
- **THEN** the returned descriptor is open, and the caller's existing cleanup closes it once
