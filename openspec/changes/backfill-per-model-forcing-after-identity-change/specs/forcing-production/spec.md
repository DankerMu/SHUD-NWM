# forcing-production Specification Delta

## ADDED Requirements

### Requirement: Per-model forcing is restored by replaying production, never by copying

Forcing for an already-produced cycle MUST be restored by re-running the producer under
the new `model_id` whenever a model's identity changed without its station bindings
moving. The superseded package MUST NOT be copied or byte-rewritten into the new model's
path.

The restoration MUST prove equivalence against the superseded package: the SHUD station
inputs MUST match byte for byte, and the data members that carry identity strings MUST
match once those strings are normalised away. Package manifests carrying member checksums
are excluded from that comparison, because their bytes must differ when the members' do.

#### Scenario: An identity-only change is replayed and verified

- **WHEN** a model's `model_id` changed and its `station_bindings` are identical after the
  identity prefix is removed
- **AND** a cycle holds forcing under the old `model_id` but not the new one
- **THEN** the producer is run for that `(source, cycle, model_id)`
- **AND** every `shud/` station file in the new package is byte-identical to the old one
- **AND** the tsd, debug, and payload members are identical after identity normalisation
- **AND** the receipt records the work item as verified

#### Scenario: Moved stations are refused rather than backfilled

- **WHEN** a model's `model_id` changed and its normalised `station_bindings` still differ
  from the superseded model's
- **THEN** no forcing is produced for that model
- **AND** the receipt records it as a skipped re-binding
- **AND** the command exits non-zero

#### Scenario: A topology diff is refused before any production

- **WHEN** the two registry manifests do not describe the same `(sp_att_path, source_id)` set
- **THEN** the command refuses with a diagnosable error naming the divergent keys
- **AND** no producer invocation is made

#### Scenario: An upper-cased source is found under its lower-cased path

- **WHEN** a renamed model's canonical source id is upper-cased
- **THEN** its existing forcing is discovered under the lower-cased path segment
- **AND** the work item is planned rather than silently omitted

### Requirement: The restoration reports what it could not cover, and never leaves an unverified package live

A restoration scan MUST NOT report an absence it never looked at as an absence of work, and MUST
NOT leave on the live model path any package that failed its own acceptance oracle. An existing
package that does not verify is work, not a completed item; a package this tool produced and could
not verify is debris, not an artifact.

#### Scenario: A partially written target package is discovered, not skipped

- **WHEN** a cycle already holds a directory under the new `model_id` that does not pass the
  equivalence check (a producer killed mid-write leaves one, because producer writes are atomic
  per file and not per directory)
- **THEN** the work item is reported with a status distinguishing it from a verified backfill
- **AND** the command exits non-zero
- **AND** the existing directory is left exactly as found unless an explicit opt-in replacement
  flag was given
- **AND** an existing package that DOES verify is still skipped

#### Scenario: A forcing root that holds nothing refuses instead of reporting no work

- **WHEN** renamed models exist and the forcing root is not a directory, or not one renamed
  model's source directory exists beneath it
- **THEN** the command refuses with a diagnosable error naming the paths it probed
- **AND** no producer invocation is made

#### Scenario: The receipt records the coverage of the scan

- **WHEN** a restoration scan completes
- **THEN** the receipt records which source directories were probed, which of them were found,
  and how many superseded model directories were seen
- **AND** partial under-coverage is therefore legible without re-deriving the paths by hand

#### Scenario: A package that fails verification is quarantined off the live path

- **WHEN** a replayed package fails the equivalence check, or the producer exits non-zero leaving
  a directory behind
- **THEN** that directory is moved out of the live `<basin_version_id>/<model_id>/` path to a name
  that cannot be mistaken for a valid model directory
- **AND** the receipt records the quarantine path for that work item
- **AND** the command exits non-zero

#### Scenario: One item's failure does not discard the receipt

- **WHEN** processing one work item raises an unexpected exception
- **THEN** that item is recorded with an error status carrying the exception
- **AND** the receipt still reports every other item's status and is still written to the
  requested output path
- **AND** the command exits non-zero

### Requirement: A run that failed on a missing artifact is restarted only through the manual-retry marker

Restarting a forecast run that failed on an absent forcing package MUST go through the
policy-gated manual-retry marker, scoped to one named run. Such a run is classified as a
permanent failure and does not resume when the package is restored; journal rows MUST NOT
be edited to achieve the same effect.

#### Scenario: The marker targets the per-run row, not the cohort master

- **WHEN** an operator previews a manual retry for a failed run whose cycle also has a
  forecast cohort-master job
- **THEN** the preview names the per-run job row
- **AND** no marker is written without an explicit execute flag

#### Scenario: A marked run is selected again by the next pass

- **WHEN** a manual-retry marker has been recorded for a run that failed with a missing
  forcing artifact
- **AND** the forcing package now exists under that run's `model_id`
- **THEN** the next scheduler pass no longer reports the candidate as permanently blocked
- **AND** runs that were not marked remain blocked

#### Scenario: Refusals are reported, not raised

- **WHEN** the named run has an active job, or has no eligible failed job
- **THEN** the command reports the refusal reason in its receipt
- **AND** exits non-zero without writing a marker
