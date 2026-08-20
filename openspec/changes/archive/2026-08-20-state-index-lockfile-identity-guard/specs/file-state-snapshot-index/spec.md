## ADDED Requirements

### Requirement: The state-index copyback merge SHALL refuse before acquiring any lock when both provider lockfiles are one file

The copyback merge SHALL determine, before acquiring the source provider destination lock, whether the source and destination provider lockfiles denote the same file, and SHALL fail closed with a structured state-manager error when they do, rather than blocking forever on a non-reentrant lock it already holds.

Sameness SHALL be decided by two complementary tests, either of which is sufficient. The first compares the `(st_dev, st_ino)` identity of the two index files' parent directories together with the two lockfile basenames, and therefore holds whether or not the lockfiles have been created yet. The second compares the `(st_dev, st_ino)` identity of the lockfiles themselves and applies only when both already exist, catching hardlinked lockfiles in genuinely distinct directories that the first test cannot see.

A path that does not exist SHALL make its test inapplicable rather than raise, on either side and for either test, because a nonexistent directory or lockfile has no inode and cannot alias an existing one. This explicitly covers the bootstrap copyback, where the destination index's parent directory is created by the lock acquisition itself and therefore does not exist when the guard runs; that merge SHALL continue to succeed exactly as it does today. Every other probe failure SHALL fail closed with the same structured error rather than being swallowed or degraded into a permissive pass.

#### Scenario: Hardlinked lockfiles in distinct directories are refused instead of self-deadlocking

- **GIVEN** a source index and a destination index in two genuinely distinct directories
- **AND** their two provider lockfiles are hardlinked to one inode
- **WHEN** the copyback merge is invoked
- **THEN** it fails closed with a structured state-manager error naming the lockfile collision
- **AND** it returns within a bounded timeout instead of blocking on the second lock acquisition
- **AND** no provider lock is acquired and neither index is modified

#### Scenario: Aliased parent directories are refused before the lockfiles exist

- **GIVEN** a source index and a destination index whose parent directories report the same `(st_dev, st_ino)` identity
- **AND** neither provider lockfile has been created yet
- **WHEN** the copyback merge is invoked
- **THEN** it fails closed with the same structured error
- **AND** the refusal happens before any lock acquisition

#### Scenario: The bootstrap copyback is not refused

- **GIVEN** a destination index whose parent directory does not exist yet, as on the first copyback into a fresh copyback root
- **WHEN** the copyback merge is invoked
- **THEN** the guard treats the absent path as an inapplicable test rather than a probe failure
- **AND** the merge proceeds and creates the destination index exactly as it does today

#### Scenario: Genuinely distinct lockfiles keep today's behavior

- **GIVEN** a source index and a destination index whose parent directories have distinct identities and whose lockfiles, if present, are distinct inodes
- **WHEN** the copyback merge is invoked
- **THEN** the merge proceeds exactly as before this change
- **AND** the outer lock is still acquired with its existing blocking semantics

### Requirement: Both copyback callers SHALL classify the pre-lock refusal as failed-closed rather than commit-uncertain

The refusal and any probe-failure wrap SHALL be raised as structured state-index errors carrying a named reason and carrying no commit phase, and both production copyback callers SHALL report them as pre-commit failures, because nothing has been read, written, or locked when the guard fires.

The run-tree copyback caller classifies by phase, so a phase-free error already lands in its failed-closed bucket. The replay tool classifies by a reason allowlist instead, so every reason this guard can raise SHALL be added to that allowlist; otherwise the replay tool would report an untouched shared index as possibly committed, run its committed-tail verification, and write an uncertain receipt.

The evidence carried by these errors SHALL use the module's existing state-index evidence treatment for local paths rather than embedding raw absolute paths in the error message.

#### Scenario: A pre-lock refusal is classified failed-closed by the run-tree caller

- **GIVEN** the lockfile-identity guard refuses a merge
- **WHEN** the run-tree copyback caller classifies the raised error
- **THEN** it reports the failed-closed state-index code
- **AND** it does not report the commit-uncertain code

#### Scenario: A pre-lock refusal is classified as a refusal by the replay tool

- **GIVEN** the lockfile-identity guard refuses a merge invoked through the replay tool
- **WHEN** the replay tool classifies the raised error
- **THEN** it reports a refusal rather than an uncertain commit state
- **AND** it does not run the committed-destination verification
- **AND** it does not write a receipt claiming an uncertain commit state
