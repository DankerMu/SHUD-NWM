## ADDED Requirements

### Requirement: Every file-provider writer participates in one fail-safe transaction

The system SHALL refresh every node-22 provider before its 168-hour consumer
limit and SHALL serialize canonical destination replacement with an expected-
preimage check shared by refresh, manual, lifecycle, readiness, and state
writers.

#### Scenario: Valid full inventory is published

- **WHEN** the authoritative Basins source validates as the exact current live
  inventory (19 models on 2026-07-15 after removing duplicate
  `HHe-MAIN-02`) and the destination-derived lock is
  acquired
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

#### Scenario: Immutable package versions follow complete source content

- **WHEN** the same publishable Basins model is discovered under another host
  root or bounded repair-run workspace with byte-identical content
- **THEN** its package version is unchanged and contains no absolute path or
  object-URI identity
- **AND** changing any required file, optional SHUD runtime file, CALIB file, or
  forcing CSV changes the package version before publication
- **AND** the publisher recomputes the planned identity and fails before object
  writes if the source changes between version planning and publication
- **AND** a repaired kashigeer-style model never reuses an existing immutable
  base version when the actual package or forcing content differs.

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

#### Scenario: Concurrent authoritative generation is never rollback-owned

- **WHEN** any worker-mirror, shared-registry, readiness, or state publisher
  reports a typed expected-preimage conflict after an authoritative writer
  installs a newer generation
- **THEN** that lane supplies no transaction commit token and is excluded from
  rollback
- **AND** every earlier lane whose atomic postimage token still matches is
  restored in reverse order
- **AND** the concurrent bytes remain unchanged and the receipt reports
  `failed/provider_preimage_changed`
- **AND** an unknown write-after exception without a matching atomic postimage
  token reports `replace_uncertain` instead of guessing rollback ownership.

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

#### Scenario: Canonical readiness is rebuilt from current catalog truth

- **WHEN** the same-run prospective registry contains the bounded production
  model set and private GFS/IFS canonical cycle directories are scanned
- **THEN** the newest cycle catalog for each source is selected with bounded
  no-follow discovery and fully validates schema, source/cycle, uniform lineage
  identities, forecast hours, canonical completeness, object containment,
  existence, and checksums
- **AND** the publisher creates exactly one readiness entry per source and
  prospective registry model, with empty inline products and exact
  `catalog_uri`, `catalog_sha256`, and `catalog_row_count` binding
- **AND** registry/readiness model sets are cross-checked before any canonical
  provider commit and only fully validated entries are passed to
  `publish_canonical_readiness_index`
- **AND** an invalid newest catalog fails closed without older-catalog fallback
  or legacy readiness-entry renewal.

#### Scenario: Bound readiness consumer identity mismatch is recomputed

- **WHEN** the caller's current policy/object identity differs from the cached
  readiness entry identity
- **THEN** the consumer re-reads the exact bound catalog and verifies URI,
  checksum, row count, products, and object checksums before recomputing
  canonical readiness against the caller identity
- **AND** a changed catalog, unmatched lineage, or missing object fails closed;
  cached identity alone never admits the candidate.

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

- **WHEN** a newest readiness catalog or state index is missing, malformed,
  symlinked, over-limit, checksum/identity-invalid, references a missing/
  mismatched object, or cannot reconstruct its bounded entries
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
  rebuilt from the newest private GFS/IFS catalogs and that same registry model
  set, and state entries are revalidated through checkpoint objects using the
  specified publishers
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

### Requirement: Registry refresh classifies every row against the prior canonical registry

The refresh runner SHALL diff the prospective registry rows against the
previous canonical `manifest-last.json` before canonical replacement and SHALL
publish a bounded per-model classification into the v1 receipt.

#### Scenario: Adding a new basin does not disturb existing model identities

- **WHEN** the prospective registry is a superset of the previous canonical
  registry (one or more model IDs added, every previously canonical model still
  present with identical `model_package_uri`, `manifest_uri`,
  `package_checksum`, `source_inventory_checksum`, and model identity fields)
- **THEN** the refresh classifies each new model as `added`, each preserved
  model as `unchanged`, and no model as `package_changed` or `refused`
- **AND** the receipt lists every classification group with bounded model IDs
  and reports the previous and new canonical registry SHA-256
- **AND** existing readiness/state and Slurm mirror generations continue to
  bind the byte-identical existing rows.

#### Scenario: Absent previous canonical registry is a legitimate first publication

- **WHEN** no prior `manifest-last.json` exists (bootstrap or explicit
  operator-initiated rebuild after the previous canonical bytes have been
  archived)
- **THEN** every prospective row is classified as `added`
- **AND** the receipt records `previous_registry_sha256: null` alongside the
  new canonical SHA-256.

### Requirement: Existing-model package checksum drift requires an explicit cutover declaration

The refresh runner SHALL refuse canonical replacement whenever any previously
canonical model appears in the prospective registry with a different
`package_checksum` unless a schema-validated cutover declaration accepts that
specific transition.

#### Scenario: Undeclared package_changed row is refused before commit

- **WHEN** at least one prospective row has the same `model_id` as a previous
  canonical row but a different `package_checksum`, and no matching cutover
  declaration is present
- **THEN** the runner exits non-zero with reason
  `registry_cutover_undeclared` before any canonical provider replace
- **AND** the previous canonical registry file's SHA-256, content, inode,
  mtime, and consumer-visible identity remain unchanged
- **AND** the receipt records every refused model with its previous and
  proposed package checksum, without leaking absolute local paths or
  credentials.

#### Scenario: Removed previously canonical model is refused

- **WHEN** a `model_id` present in the previous canonical registry is missing
  from the prospective registry
- **THEN** the runner classifies that model as `removed` and refuses canonical
  replacement with reason `registry_cutover_removal_refused`
- **AND** the previous canonical bytes remain unchanged; deliberate
  decommission requires a separate declared workflow outside this refresh.

#### Scenario: Valid cutover declaration accepts a specific package cutover

- **WHEN** a `nhms.scheduler.registry_package_cutover.v1` declaration is
  configured, its `generation` equals the prospective registry generation, each
  entry names an existing `model_id` with matching `old_checksum` (equal to the
  previous canonical `package_checksum`) and matching `new_checksum` (equal to
  the prospective `package_checksum`), `effective_cycle_utc` is aligned to
  either 00:00 or 12:00 UTC, and `transition_mode` is a supported value
- **THEN** the runner classifies the row as `package_changed` under
  `declared_cutovers`, allows canonical replacement, and lists the accepted
  declaration in the receipt.

#### Scenario: Invalid cutover declaration fails closed

- **WHEN** any of the following holds: the declaration file schema is invalid;
  a declaration `model_id` is absent from the prospective registry; the
  declaration's `old_checksum` mismatches the previous canonical
  `package_checksum` or its `new_checksum` mismatches the prospective
  `package_checksum`; the declaration `generation` does not equal the
  prospective registry generation; `effective_cycle_utc` is not aligned to
  00:00 or 12:00 UTC; the declaration contains duplicate model IDs; the
  declaration `effective_cycle_utc` is in the past by more than the
  configured tolerance (24 hours) or unreachably far in the future (more than
  168 hours); the declaration file is symlinked, non-regular, or exceeds the
  bounded read limit
- **THEN** the runner fails closed with reason
  `registry_cutover_declaration_invalid` before canonical replacement
- **AND** the previous canonical registry file's bytes, digest, and identity
  remain unchanged.

### Requirement: Receipt reports bounded per-model registry classification

The v1 receipt SHALL include a `registry_classification` object on every
`dry_run`, `published`, and refusal outcome that reflects the diff between the
previous canonical registry and the prospective registry.

#### Scenario: Classification counts are bounded and sanitized

- **WHEN** the runner completes any outcome that touched classification
- **THEN** `registry_classification` reports `added`, `unchanged`, `removed`,
  `package_changed`, `refused`, and `declared_cutovers` as bounded arrays with
  the previous and new canonical registry SHA-256
- **AND** each list caps at 256 entries with a `truncated` boolean and exact
  `total`
- **AND** entries expose only `model_id`, `old_checksum`, `new_checksum`, and
  matching declaration identity; absolute filesystem paths, absolute object
  URIs, and credentials never appear
- **AND** counts reconcile the previous and prospective model sets:
  `unchanged + package_changed + removed` equals the previously canonical
  model count, `added + unchanged + package_changed` equals the prospective
  model count, `declared_cutovers` is a subset of `package_changed`, and
  `refused` equals every `removed` entry plus every `package_changed` entry
  not present in `declared_cutovers` plus every entry rejected by
  `registry_cutover_declaration_invalid`.

#### Scenario: 19-model live inventory records the actual classification shape

- **WHEN** the current 19-model live inventory refreshes against a 13-model
  previous canonical registry after adding six new basin models
- **THEN** the receipt records exactly six `added` entries, exactly thirteen
  `unchanged` entries (byte-identical model/package identities), zero
  `package_changed`, zero `refused`, and zero `declared_cutovers`
- **AND** an intentional simultaneous package cutover for one existing model
  produces one `declared_cutovers` entry and twelve `unchanged` entries with
  the same `added` count.
