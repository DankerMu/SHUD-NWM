## ADDED Requirements

### Requirement: Every file-provider writer participates in one fail-safe transaction

The system SHALL refresh every node-22 provider before its 168-hour consumer
limit and SHALL serialize canonical destination replacement with an expected-
preimage check shared by refresh, manual, lifecycle, readiness, and state
writers.

#### Scenario: Valid full inventory is published

- **WHEN** the authoritative Basins source validates as the current 13-model
  inventory and the destination-derived lock is acquired
- **THEN** `publish_all_basin_scheduler_registry` validates packages and invokes
  `publish_scheduler_registry_manifest`
- **AND** readers observe a complete manifest-last payload bound to schema,
  checksum, generation time, model identities, and object evidence
- **AND** immutable registry packages exist only under the private
  `OBJECT_STORE_ROOT`, while the shared provider root contains only the three
  canonical provider files and their lock files
- **AND** the unchanged scheduler consumer resolves every registry package
  reference against the private root and fails closed if a referenced private
  package disappears
- **AND** a bounded terminal receipt is bound to those exact bytes.

#### Scenario: Timer manual and lifecycle writers contend

- **WHEN** any two canonical registry writers overlap
- **THEN** exactly one owns the shared destination-derived lock
- **AND** the contender replaces no canonical bytes and emits no success
- **AND** the existing manual CLI arguments and successful output remain
  compatible when no contention exists.

#### Scenario: Readiness or state changes during renewal

- **WHEN** an authoritative readiness/state writer replaces a destination after
  refresh snapshots it but before renewal commits
- **THEN** the common destination write lock compares the current digest/inode
  to the refresh expected preimage before replace
- **AND** refresh exits `provider_preimage_changed` without overwriting the new
  authoritative entries
- **AND** writers acquire only one destination lock at a time, so the runner
  needs no multi-lock ordering and cannot deadlock across three providers.

#### Scenario: Pre-commit failure preserves canonical identity

- **WHEN** configuration, discovery, package/reference validation, staging, or
  pre-commit verification fails before atomic canonical replacement
- **THEN** the previous manifest bytes, digest, inode, mode, owner/group, mtime,
  and consumer-visible identity remain unchanged
- **AND** immutable content-addressed packages already published by the existing
  publisher are bounded orphan candidates recorded in evidence and are never
  automatically deleted.

#### Scenario: Replace and post-commit failures are phase explicit

- **WHEN** replace/fsync, post-commit re-read, rollback, or receipt publication
  fails after canonical replacement may have occurred
- **THEN** the outcome is exactly `replace_uncertain`, `restored_previous`, or
  `published_receipt_failed`, never a false pre-commit preservation claim
- **AND** a certain invalid replacement may restore captured validated previous
  bytes atomically without claiming the old inode/mtime survived
- **AND** an uncertain or receipt-only failure exits non-zero and leaves the
  unchanged consumer to validate complete old-or-new bytes.

#### Scenario: Concurrent readers and temp cleanup remain safe

- **WHEN** discovery overlaps temp write/fsync/replace/rollback
- **THEN** readers see complete old or complete new JSON, never a partial file
- **AND** current-run temp files are removed only with certain identity;
  uncertain cleanup is capped safe root-relative residue.

### Requirement: Every expiring DB-free provider is renewed from validated truth

The refresh lifecycle SHALL cover the registry, canonical-readiness index, and
state index without a database fallback or timestamp-only edit.

#### Scenario: Canonical readiness valid except age is renewed

- **WHEN** the bounded readiness payload is valid except `generated_at`
- **THEN** the publisher bypasses only freshness while `_validate_readiness_index`
  rechecks schema/checksum, source-cycle-model-basin-product identities,
  forecast hours, catalog/object containment, existence, and checksums
- **AND** only fully validated entries are passed to
  `publish_canonical_readiness_index`.

#### Scenario: State index valid except age is renewed

- **WHEN** the bounded state index is valid except `generated_at`
- **THEN** `FileStateSnapshotIndexRepository` loads it with freshness disabled
  and object verification enabled
- **AND** only fully validated entries are passed to
  `publish_state_snapshot_index`.

#### Scenario: State checkpoint copyback precedes shared index publication

- **WHEN** a private lifecycle state index contains a checkpoint not yet
  present in the shared NFS object tree
- **THEN** copyback copies and checksum-verifies the checkpoint under the
  shared `states/` key before publishing the merged shared canonical index
- **AND** the shared leaf is mode 0664 under mode-0775 created directories,
  while a checkpoint-copy failure preserves the prior shared index
- **AND** copyback and refresh serialize on the same state-index destination
  lock so one contender fails closed without deadlock or lost entries.

#### Scenario: Invalid provider evidence is never renewed

- **WHEN** an index is missing, malformed, over-limit, checksum/identity-invalid,
  references a missing/mismatched object, or cannot reconstruct its bounded
  entries
- **THEN** refresh fails before scheduler admission and preserves prior bytes
- **AND** it publishes no empty replacement, `generated_at`-only edit, stale
  acceptance, or DB-derived fallback.

### Requirement: Refresh execution is bounded database-free and auditable

The service SHALL use only trusted node-22 repository/Basins/object-store/file-
provider paths and SHALL emit a bounded receipt without secrets.

#### Scenario: Effective service environment is database-free

- **WHEN** user-systemd starts the service
- **THEN** no `DATABASE_URL` or libpq selector is configured and no connection
  to historical `:55433` is attempted
- **AND** the receipt records only a redacted DB-free boolean proof.

#### Scenario: Unsafe paths fail before publication

- **WHEN** env/interpreter/repo/lock/receipt/work/Basins/object/provider paths are
  relative, symlinked where prohibited, non-regular, or outside their trusted
  containment/ownership boundary
- **THEN** the runner fails with a closed sanitized reason before publication.

#### Scenario: Receipt workspace and service bounds are enforced

- **WHEN** any terminal outcome is produced
- **THEN** it conforms to
  `nhms.scheduler.file_provider_refresh_receipt.v1` with only `dry_run`,
  `published`, `already_running`, `failed`, `replace_uncertain`,
  `restored_previous`, or `published_receipt_failed`
- **AND** reason tokens are closed and at most 64 characters, serialized bytes
  are at most 1 MiB, collections at most 256 items, strings at most 512
  characters, and residues at most 64 safe relative entries
- **AND** latest receipt publication is atomic, history keeps the newest 32,
  certain current-run workspace cleanup is bounded, and service timeout is at
  most two hours
- **AND** one run is capped at 64 GiB, 250,000 filesystem entries and depth 32;
  at most 4,096 immutable orphan candidates may be created, while receipt
  evidence stores the first 256 plus exact total and `truncated=true`
- **AND** exceeding a workspace/orphan bound fails before canonical commit and
  performs only identity-certain current-run cleanup.
- **AND** concurrent receipt publishers retain exactly the chronologically
  newest 32 valid history records and an older completion cannot replace a
  newer `latest.json`.

#### Scenario: Receipt failure after provider commit has durable fallback

- **WHEN** the primary latest/history receipt cannot be published after one or
  more provider commits
- **THEN** a separately preflighted local-filesystem emergency slot reserved by
  exclusive create before any provider commit receives a bounded fsynced v1
  `published_receipt_failed` record bound to the committed provider digests
- **AND** reservation fsyncs both the regular file and its pinned parent
  directory before the first provider side effect, finalization handles short
  writes to completion, verifies exact bytes, and fsyncs file then parent
- **AND** the operator recovery command validates those digests and reconstructs
  primary latest/history from that record without republishing provider data
- **AND** failure to finalize both primary and reserved emergency evidence is
  `replace_uncertain`, exits non-zero, and requires direct provider validation;
  journal/stderr alone is never claimed as authoritative acceptance evidence.

### Requirement: Timer deployment is reversible and has a safe steady state

The repository SHALL provide byte-identifiable service/timer/env-example units
whose cadence plus jitter is strictly below 168 hours.

#### Scenario: Static unit contract is valid

- **WHEN** unit/env static tests run
- **THEN** the service uses absolute node-22 repo/interpreter paths, the mode-
  0600 DB-free env, private lock/work/receipt locations, journal output and a
  bounded timeout
- **AND** it rejects/unsets the complete libpq connection environment surface,
  keeps the three shared provider destinations distinct from private registry-package
  and referenced-object storage, and does not trigger a missed-run catch-up during installation
- **AND** changing refresh units does not change scheduler units.

#### Scenario: Failed deployment restores initial state

- **WHEN** install, refresh, provider validation, or live proof fails
- **THEN** refresh units/timer return to their recorded initial state,
  scheduler timer returns to its initial enabled/active state, services are
  inactive between ticks, and no issue-owned Slurm job remains.

#### Scenario: Enable validates exact current receipt before mutation

- **WHEN** install `--enable` or repeated `--enable` is requested
- **THEN** the installer first requires both refresh units to be exactly
  inactive and validates one bounded no-follow current v1 published receipt
  whose ordered three provider digests match current canonical bytes
- **AND** missing, stale, minimal, symlinked, oversized, non-published, or
  digest-mismatched receipt evidence causes no unit-file or unit-state mutation
- **AND** `activating`, `deactivating`, `reloading`, or `active` service state is
  refused without stopping the service; a later failure restores that
  invocation's exact enabled/active entry state.

#### Scenario: Successful deployment establishes refresh steady state

- **WHEN** manual refresh and live proof pass
- **THEN** scheduler timer returns to its recorded initial state
- **AND** the newly installed refresh timer remains enabled/active on the
  documented cadence with both oneshot services inactive between ticks.

### Requirement: Live recovery proves actual multi-stage writer behavior

The change SHALL NOT complete until a real production pass reaches terminal
Slurm evidence and produces new shared-NFS artifacts visible from node-27.

#### Scenario: All stale prerequisites are authoritatively renewed

- **WHEN** live preflight finds stale registry/readiness/state artifacts
- **THEN** registry is rebuilt from `NHMS_BASINS_ROOT`, readiness entries are
  revalidated through their catalogs/objects, and state entries through their
  checkpoint objects using the specified publishers
- **AND** before/after schema/checksum/generation/entry identity evidence proves
  no timestamp forgery or freshness relaxation.

#### Scenario: One pass run may span actual stage jobs

- **WHEN** the provider set admits an eligible pass
- **THEN** pass/candidate/run identity binds every actual Slurm stage job and at
  least one job reaches a recorded terminal outcome
- **AND** actual writer stage(s) create a genuinely new forcing leaf, runs leaf,
  and states leaf; reuse of old forcing does not satisfy this proof
- **AND** no `db_free_registry_blocked`, DB connection, or synthetic ACL probe is
  accepted as success.

#### Scenario: Node-27 proves shared-NFS identity and inheritance

- **WHEN** the writer chain completes
- **THEN** node-27 observes the exact new run/source/model/cycle leaves on the
  same NFS and verifies owner/group/mode/default ACL plus `nwm` access
- **AND** scheduler/unit/job restoration and refresh steady state are recorded.
