# timeseries-product-archive Specification (delta)

## ADDED Requirements

### Requirement: Archive tier location and object layout

Archive objects SHALL live under the rotation-exempt archive root
`NHMS_ARCHIVE_ROOT` (node-27: `/home/ghdc/nwm/archive`; node-22 view:
`/ghdc/data/nwm/archive`), organized per (source, cycle[, basin/run]) as a
`tar.zst` tarball plus a sidecar `manifest.json` recording the file list,
per-file sha256 checksums, file sizes, tarball sha256, and identity fields
(source, cycle_time, basin/run identifiers, created_at, tool version).

#### Scenario: Cycle product directory is archived

- **WHEN** the archive mover archives an aged cycle product directory from
  `object-store/forcing/` or `object-store/runs/`
- **THEN** a `tar.zst` object and `manifest.json` MUST exist under
  `NHMS_ARCHIVE_ROOT` with recorded checksums and identity fields

#### Scenario: State snapshot products are archived

- **WHEN** the archive mover archives an aged state product from
  `object-store/states/` that is referenced by `state_snapshot.state_uri`
- **THEN** it MUST receive the same treatment as forcing and run products: a
  `tar.zst` object plus `manifest.json` under `NHMS_ARCHIVE_ROOT` with
  recorded checksums and identity fields, verified before source deletion

#### Scenario: Source-less legacy state snapshot remains archivable

- **WHEN** a valid state row has `source_id = NULL` and references the legacy
  `states/<model>/<valid-time>/...` object layout
- **THEN** archive provenance MUST use the explicit states-only canonical
  source `legacy-unqualified`, with cycle time derived from the required
  state `valid_time`
- **AND** its archive path MUST be deterministic and collision-disjoint from
  provider-qualified state paths
- **AND** forcing/runs MUST reject that sentinel and no provider identity may
  be synthesized

#### Scenario: Archive root is exempt from rotation

- **WHEN** any retention or cleanup tool (including raw retention) resolves
  its target roots
- **THEN** paths under `NHMS_ARCHIVE_ROOT` MUST NOT be eligible deletion
  targets, enforced by configuration validation that rejects overlapping
  roots

### Requirement: Staged atomic writes and verify-before-delete

The archive mover SHALL write each archive object to a same-volume staging
path and atomically rename it to its final path only after the object's
checksums have been verified by re-reading the written tarball; it SHALL NOT
remove source product files until that verification has passed. An object at
the final path counts as present — for idempotency skips and for the
archive-completeness receipt — only when it verifies against its manifest.

#### Scenario: Checksum verification fails

- **WHEN** the written tarball's sha256 does not match the manifest
- **THEN** the source directory MUST remain untouched
- **AND** the receipt MUST record the failure and the process MUST exit
  non-zero

#### Scenario: Idempotent re-run

- **WHEN** the mover encounters a cycle whose verified archive object already
  exists
- **THEN** it MUST skip re-archiving without duplicating objects and record
  the skip in the receipt

#### Scenario: Residual unverified object at the final path

- **WHEN** a run encounters an existing final-path object that fails
  manifest/checksum verification (for example residue of an interrupted
  earlier run)
- **THEN** the mover MUST treat the cycle as not archived: quarantine the
  failing object out of the final path, re-archive via fresh staging, and
  record the quarantine in the receipt
- **AND** the source directory MUST remain untouched until the replacement
  object verifies

### Requirement: Bounded, locked, receipted operation with explicit age eligibility

The archive mover SHALL run single-instance (flock), process at most a
configurable number of cycles per tick, default to dry-run, require an
explicit enforce flag for mutation, and emit a JSON receipt (candidates,
actions, bytes moved, skips, failures) per run. Archive candidates SHALL be
limited to cycles older than a configurable minimum age
(`NHMS_ARCHIVE_MIN_AGE_DAYS`, default 45 days); configuration validation
SHALL reject a minimum age below the DB retention window (30 days), so the
hot object-store window — which is also the ADR 0001 display disk window for
station forcing CSVs — is never shorter than the DB hot window. Cycles
rotated after the minimum age are thereafter reachable only via the archive
tier (display routes return their ADR 0001 not-found for them).

#### Scenario: Previous run still active

- **WHEN** a tick starts while the flock is held
- **THEN** the new tick MUST exit without mutating and note the skip

#### Scenario: Dry-run lists without mutating

- **WHEN** the mover runs without the enforce flag
- **THEN** it MUST emit the candidate list and planned actions in the receipt
  and perform no filesystem mutation

#### Scenario: Cycle younger than the minimum age is not archived

- **WHEN** a cycle product directory's cycle time is younger than the
  configured minimum archive age
- **THEN** the mover MUST NOT select it as a candidate and it MUST remain in
  the hot object-store

#### Scenario: Minimum age below the retention window is rejected

- **WHEN** configuration sets the minimum archive age below the DB retention
  window
- **THEN** configuration validation MUST reject the configuration before any
  mutation

### Requirement: Governance capacity visibility

The node-27 resource-governance audit SHALL report the archive root size and
shared-volume free space, and the archive mover SHALL refuse enforce mode
when free space is below a configurable threshold.

#### Scenario: Free space below threshold

- **WHEN** the shared volume's free space is below the configured refuse
  threshold
- **THEN** the mover MUST refuse enforce mode, leave sources untouched, and
  emit a receipt warning

### Requirement: Inventory audit emits the archive-completeness receipt

The inventory audit SHALL compare DB coverage — `hydro_run` cycles,
`forcing_version` windows, and `state_snapshot.state_uri` references —
against checksum-verified archive objects and hot object-store presence, and
emit a JSON **archive-completeness receipt** recording: `generated_at`, the
inventoried coverage bounds, a verdict for every inventoried subject, and the
salvage selector list. Each verdict SHALL bind exactly one lane-discriminated
stable subject (`forcing_version_id`, `run_id`, or `state_id`) so subjects
sharing a time window remain distinguishable; the coverage mechanism SHALL
be recorded separately from the subject lane. The verdict per subject SHALL
be `complete` when it is covered by a
checksum-verified product archive object or a verified `db-export` salvage
object, or when its products are present in the hot object-store and the
window is not yet past the archive minimum age; `pending-archive` when past
the minimum age with hot-object-store-only products; and `gap` when no copy
exists. Every salvageable forcing/river timeseries `gap` appears in the
salvage selector list. A state subject MUST NOT claim `db-export` coverage:
its missing product remains a non-salvageable `gap` that blocks retention
until product coverage is restored. This
receipt is the single artifact named "archive completeness receipt" consumed
by the `timeseries-db-retention` enforce gate and the scope source consumed
by `db-export-salvage`. The audit SHALL run recurringly from a node-27
user-level systemd timer registered in the resource-governance audit unit
list, so a fresh receipt is available to each retention tick.

#### Scenario: Verified archive coverage yields a complete verdict

- **WHEN** the audit inventories a window whose products exist as
  checksum-verified archive objects (or verified `db-export` salvage objects)
- **THEN** the receipt MUST mark that window `complete` and exclude it from
  the salvage selector list

#### Scenario: Past-eligibility window without a verified archive object

- **WHEN** a window is older than the archive minimum age but its products
  exist only in the hot object-store (no checksum-verified archive object)
- **THEN** the receipt MUST mark it `pending-archive`, and that verdict MUST
  NOT satisfy the retention gate's completeness check for any drop window
  containing it

#### Scenario: Salvageable DB-only timeseries window is a gap with selectors

- **WHEN** the audit finds forcing or river timeseries rows whose upstream
  products exist in neither the hot object-store nor the archive
- **THEN** the receipt MUST mark that window `gap` and include its exact
  selectors in the salvage selector list

#### Scenario: Missing state artifacts cannot use DB-export salvage

- **WHEN** a `state_snapshot` reference is absent from both the hot object
  store and verified product archive
- **THEN** its subject verdict MUST remain `gap`
- **AND** the receipt MUST NOT claim `db-export` coverage or fabricate a
  timeseries salvage selector for that state

#### Scenario: Equal-window subjects remain independently auditable

- **WHEN** two inventory subjects share the same time window but have
  different identities or coverage outcomes
- **THEN** the receipt MUST carry distinct subject-bound verdicts for both
- **AND** a missing or cross-lane subject identity MUST fail schema validation
- **AND** the runtime audit MUST reject duplicate or omitted inventory
  subjects before publishing a retention-gating receipt
