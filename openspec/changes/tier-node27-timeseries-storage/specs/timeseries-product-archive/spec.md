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

The mover SHALL discover filesystem-only archive units by strict lane shape:
one forcing basin/model leaf, one whole run-id tree whose bounded input
manifest is authoritative for source/cycle/model/basin, or one physical state
valid-time tree. Provider-qualified and `legacy-unqualified` state identities
SHALL remain collision-disjoint. Historical run-id spelling SHALL NOT replace
manifest identity validation, and the mover SHALL NOT access the database or
fabricate clone-target archives for a shared physical state artifact. Run
top-level identity, nested model identity and output URIs SHALL use the same
authority as the inventory audit; duplicated `identity.*` values SHALL agree.
Run output URIs SHALL bind configured S3 scheme/bucket/optional prefix and
reject wrong authority/prefix, query/fragment, encoded traversal, backslash or
unsupported schemes.

#### Scenario: State snapshot products are archived

- **WHEN** the archive mover archives an aged state product from
  `object-store/states/` that is referenced by `state_snapshot.state_uri`
- **THEN** it MUST receive the same treatment as forcing and run products: a
  `tar.zst` object plus `manifest.json` under `NHMS_ARCHIVE_ROOT` with
  recorded checksums and identity fields, verified before source deletion

#### Scenario: Source-less legacy state snapshot remains archivable

- **WHEN** a valid state row has `source_id = NULL` (or the existing
  equivalent empty-string representation) and references the legacy
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
Re-reading SHALL decompress the staged/final tar and prove an exact bijection
with the manifest's deterministic regular-file list, including safe relative
path, size and sha256. Duplicate, missing, extra, path-traversing, symlink,
hardlink, device or FIFO members SHALL fail verification even when the
tarball-level sha256 matches. Actual member count/name/type/declared size and
depth SHALL be checked at each header before body extraction; unexpected or
oversize members fail immediately, with cumulative payload limits enforced.

The verified tarball and manifest SHALL be published as one dirfd-bound,
no-replace atomic leaf directory, followed by complete durability fsync and
final-path re-verification. Both staged files and their directory SHALL be
fsynced before rename; created ancestors and every source/destination parent
affected by publish, quarantine, tombstone or tombstone removal SHALL be
fsynced. A raced destination SHALL never be overwritten. A corrupt,
partial or unexpected final leaf SHALL be atomically quarantined whole on the
same archive device before fresh staging; dry-run only reports that plan. A
verified existing archive is idempotent only when any still-present source
tree is member-for-member identical; drift preserves both and fails rather
than overwriting valid historical evidence. Member comparison is a unique
path -> (size, sha256) map and is independent of manifest array order.
Only typed deterministic structural/schema/identity/member/checksum invalidity
permits quarantine. Operational verification failure (timeout, tool, I/O,
mount proof) preserves canonical final + source and fails; conflict is a
separate typed outcome.

Source/archive discovery, tar reads and retirement SHALL remain
descriptor-bound with no-follow component opens, fixed-root-device checks and
bounded traversal. Every opened FD SHALL match the pinned Linux mount ID as
well as device, so same-filesystem bind mounts and cross-device descendants
fail closed; production inability to prove FD mount identity is a blocker.
Traversal SHALL hold only the bounded directory stack plus one regular file
FD, with before/after fstat around the same tar/hash byte stream and a complete
second tree comparison. Immediately before
retirement the pinned source tree SHALL still equal the archived preimage.
The exact source leaf SHALL then be atomically renamed by held parent FDs into
a same-device delete tombstone and only that pinned inode tree recursively
removed after another tombstone comparison. A replacement at the original
pathname, observed late write/entry change, cross-device rename, fsync or
namespace-identity failure SHALL block deletion and SHALL NOT report the
candidate archived. Aged manifest-complete product trees are an immutable
producer contract; writes through an already-open FD after the final check are
an explicit producer violation that no rename protocol can prevent. Failures
before tombstone rename preserve the original source path; failures after
rename/unlink begins SHALL truthfully report complete/partial tombstone residue
and SHALL NOT claim the source path was untouched or automatically restored.
Before any tombstone child unlink the pinned final tar+manifest pair SHALL be
fully re-verified. Removal SHALL follow the archived preimage's exact
path/inode/signature allowlist; extra, missing or drifted entries preserve
residue and fail rather than being enumerated and deleted.

#### Scenario: Checksum verification fails

- **WHEN** the written tarball's sha256 does not match the manifest
- **THEN** the source directory MUST remain untouched
- **AND** the receipt MUST record the failure and the process MUST exit
  non-zero

#### Scenario: Idempotent re-run

- **WHEN** the mover encounters a cycle whose verified archive object already
  exists
- **THEN** it MUST avoid duplication; an identical present source is planned
  for retirement in dry-run and `retired-from-existing` in enforce
- **AND** a drifted present source is a conflict that preserves both
- **AND** archive-only identities with no source are not mover candidates and
  remain the inventory audit's responsibility

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
The Python entrypoint SHALL acquire the non-blocking flock itself before
discovery/mutation. One UTC now SHALL drive strict
`eligibility_end < now - minimum_age`; equality remains hot. Forcing/run
eligibility end is authoritative non-inverted manifest `end_time`, matching
the inventory/DB/display hot window, while state uses valid-time point.
Canonical archive identity/order remains cycle-time based and receipts also
bind eligibility end. Candidate
selection SHALL be deterministic and record all eligible work beyond the
positive tick bound as deferred. Discovery and each tree SHALL have explicit
candidate/entry/depth/manifest-size, per-file/source/tar/uncompressed-byte,
compressor-timeout and stderr limits; unsafe, unreadable, malformed or
over-limit evidence is a failure rather than a skip. The compression
executable SHALL resolve to a configured absolute regular non-symlink path
(node-27 default `/usr/bin/zstd`) and run with fixed argv and `shell=False`.
Malformed/unreadable entries that cannot form canonical identity SHALL be
separate deterministic discovery failures keyed by lane hint + safe
root-relative locator; they count toward discovery/overall failure but never
enter the canonical selected/deferred partition or processing bound.

The lock file is absolute mode-0600 safe coordination metadata opened from a
trusted dirfd with no-follow; existing files are fstat-bound without
truncation and first creation is exclusive plus parent-fsynced. Only the lock holder
may publish the shared receipt; a contender emits one structured JSON skip to
stderr and never overwrites the active holder's receipt. The JSON receipt SHALL
validate against `product_archive_receipt.schema.json`, use an absolute
configured path and the #847 strict dirfd/no-follow mode-0600 publication
protocol: exclusive temp, file fsync, atomic replace, directory fsync and
post-replace parent check; pre-replace failure preserves the old receipt and
post-replace uncertainty is indeterminate. It SHALL record the captured now/cutoff, mode/bound,
candidate/selected/deferred identities, one terminal outcome per selected
identity, ordered side events, disjoint locator-keyed discovery failures,
bytes and reasons. Legacy skipped/quarantined action arrays SHALL NOT be
alternate terminal representations. Runtime set invariants require exact valid
candidate = selected + deferred partitioning. Each selected identity has one
terminal outcome and zero or more ordered side events (including quarantine);
locator-keyed discovery failures remain disjoint. Ordering is deterministic,
bytes are non-negative and overall outcome matches terminal/discovery failures.
Lock metadata plus the receipt are the only writes allowed during dry-run.
Enforce requires an explicit flag, may continue
over bounded independent candidates, and exits non-zero if any candidate
failed or publication/retirement became indeterminate.

#### Scenario: Previous run still active

- **WHEN** a tick starts while the flock is held
- **THEN** the new tick MUST exit without mutating and note the skip
- **AND** it MUST emit a structured JSON diagnostic without touching the
  lock holder's stable receipt

#### Scenario: Dry-run lists without mutating

- **WHEN** the mover runs without the enforce flag
- **THEN** it MUST emit the candidate list and planned actions in the receipt
  and perform no source/archive/staging/quarantine mutation; atomic receipt
  publication and safe lock metadata are the sole permitted writes

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

The inventory transaction SHALL be `REPEATABLE READ READ ONLY`, use one
captured audit time, and apply a 20-second statement timeout. A forcing
version or hydro run is an inventory subject only when its
corresponding detail hypertable has at least one row; metadata-only rows SHALL
NOT generate zero-row salvage selectors. Forcing/run windows use their
authoritative inclusive metadata `start_time`/`end_time` bounds without a
full detail rescan. State references use their `valid_time` point, and
archive-age classification uses the subject window's end. Every
window MUST satisfy start <= end and receipt coverage bounds MUST exactly
equal the subject-set min(start)/max(end). Detail-presence queries SHALL use
bounded correlated identity-leading probes rather than decorrelating into a
full hypertable scan/aggregate. Forcing and run cycle times MUST resolve to an
exact UTC hour; non-zero minutes, seconds or microseconds SHALL block the
audit rather than be truncated into another cycle's archive identity.

All hot/archive/salvage paths SHALL be strictly parsed, root-contained,
regular non-symlink evidence. Forcing hot coverage requires its bounded
`forcing_package.json`, DB manifest checksum, row/URI identity, a manifest
time range containing the authoritative DB subject window, and every
manifest-listed file checksum to agree. Run hot coverage requires its bounded
input manifest identity plus at least one contained regular output file.
State hot coverage requires the referenced regular file to match the DB
checksum. Existing permission/I/O failures, malformed
URIs/manifests, containment escapes, or conflicting selector evidence SHALL
block publication rather than be treated as absence. A missing archive root
or absent canonical archive siblings is ordinary absence. Product archive
coverage requires a schema-valid, semantic-binding-valid manifest whose
declared tarball size and sha256 match the regular tarball. A fully readable
archive/salvage object with a size or checksum mismatch is known-invalid
coverage: it SHALL be reported in subject evidence and treated as absent so
classification safely continues to another copy or pending/gap. DB-export coverage
requires a schema-valid salvage manifest plus size/sha256 verification of the
exact selector's object; discovery under the archive `db-export/` namespace
SHALL be bounded and symlink-safe: at most 10,000 manifests, at most eight
levels and 100,000 total entries beneath `db-export/`, and at most 16 MiB per
manifest. Directory enumeration, entry stat, child open, manifest read and
referenced-object hash SHALL use one held descriptor-bound `db-export` tree;
the audit SHALL NOT reopen stored manifest paths after traversal. Inventory SHALL be capped at 100,000 subjects; exceeding any bound
blocks publication.
Run output traversal SHALL inspect at most 10,000 entries and eight levels per
run, failing closed on overflow while still checking all bounded siblings.
Enumeration, entry stat and child-directory opens SHALL remain bound to the
same held directory-FD tree so a pathname replacement cannot swap the
namespace between list and stat.

Forcing basin identity SHALL come from the referenced model instance, not an
arbitrary detail row. A clone state SHALL be bound in the same repeatable-read
snapshot to its existing origin state: origin model/source/time/URI/checksum
must match the declared shared artifact and the clone fingerprint must be
canonical lowercase sha256. Source-less legacy clone comparison SHALL treat
the existing `NULL` and empty-string representations as the same
`legacy-unqualified` source without equating any provider identity.

Evidence path traversal/read/hash and receipt temp/replace SHALL remain
anchored to trusted directory file descriptors with no-follow component
opens. `ENOENT` is ordinary absence only after every existing parent has been
verified as a real directory. JSON bytes, size and sha256 SHALL come from the
same opened inode; a pre-existing or raced symlink/non-directory/permission
error is a blocker.
For a state subject, including a clone, archive coverage SHALL additionally
require exactly one manifest file entry whose path is the strict physical
state URI relative to its provider/legacy state root and whose sha256 equals
the origin-bound DB checksum. A tarball with only lane/model/time identity is
not proof that the referenced state artifact is preserved.

The emitted receipt SHALL contain every inventoried stable subject exactly
once, deterministically ordered. Its forcing/run gaps and salvage selectors
SHALL form an exact bijection; state gaps SHALL have no selector. The complete
receipt SHALL pass the pinned schema before atomic replacement. Empty
inventory or any blocker SHALL exit non-zero without overwriting a previous
valid retention-gating receipt. The output path is an absolute CLI/env
contract, all parent components are non-symlink directories, and publication
uses a mode-0600 same-directory temporary file with flush/fsync, atomic
replace, mandatory directory fsync, post-replace verification that the pinned
parent FD still names the configured parent, and pre-replace failure cleanup.
Directory-fsync failure or an observed parent namespace replacement SHALL be
reported as indeterminate publication and SHALL NOT report `published`.
Failures before replace preserve the old receipt byte-for-byte; after-replace
failures must fail closed rather than falsely claim either successful
publication or preservation. The configured parent is therefore an
operator-controlled, non-rotating namespace during publication. A fully
validated, file-fsynced receipt may already be visible after an indeterminate
post-replace failure; the later retention consumer SHALL independently apply
the exactly-two-receipt content gates and SHALL NOT add producer exit status,
a sidecar marker or systemd state as a third gate. Failure
diagnostics go to stderr and never replace the gate receipt. Strict
post-replace durability/identity checks SHALL be explicitly enabled for this
receipt and SHALL NOT silently change the shared helper's default contract for
unmigrated non-receipt callers. Runtime schema validation SHALL
use a direct production dependency.
Archive minimum age SHALL reuse the shared 30-day retention safety invariant
and SHALL reject explicit zero/below-30 inputs rather than falling back.
Evidence for every readable corrupt sibling SHALL be retained regardless of
which valid coverage mechanism wins precedence. Readable hot forcing
manifest/member and state-file checksum mismatches are absent coverage with
retained evidence; malformed identity, unsafe type, permission and I/O
failures remain publication blockers.

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

#### Scenario: Metadata without detail rows is not salvage scope

- **WHEN** a forcing version or hydro run has no row in its detail hypertable
- **THEN** it MUST NOT become an inventory subject or salvage selector

#### Scenario: Unknown or unsafe evidence state blocks publication

- **WHEN** a referenced hot object or existing archive/salvage artifact is
  unreadable, unsafe, malformed, or conflicting
- **THEN** the audit MUST fail non-zero and preserve the previous valid
  receipt

#### Scenario: Readable checksum mismatch is absent coverage

- **WHEN** an otherwise readable product archive or salvage object fails its
  declared size or checksum
- **THEN** that copy MUST NOT count as verified coverage
- **AND** the mismatch MUST be recorded in the subject evidence while the
  audit continues to another verified copy, hot coverage, or pending/gap

#### Scenario: Missing archive namespace is ordinary absence

- **WHEN** the archive root or a subject's canonical archive siblings do not
  yet exist
- **THEN** the audit MUST continue classification using verified salvage,
  hot object-store presence, or `gap`

#### Scenario: Clone state shares its authoritative physical artifact

- **WHEN** a clone state row has complete clone provenance and its state URI
  names the provenance-declared physical source model
- **THEN** the receipt MUST retain the clone `state_id` as its stable subject
- **AND** hot/archive coverage MUST follow the shared physical artifact
- **AND** an undeclared model alias or source/time drift MUST block
  publication

#### Scenario: Receipt publish is all-or-nothing

- **WHEN** the subject set is empty, duplicated, incomplete, has a
  gap-selector mismatch, fails schema validation, or encounters any audit
  blocker
- **THEN** no new gate receipt may replace the previous valid receipt

#### Scenario: Audit resource bounds are enforced

- **WHEN** the inventory, manifest size, salvage manifest count, or discovery
  depth exceeds its fixed safety bound
- **THEN** the audit MUST fail non-zero without replacing the prior receipt

#### Scenario: Receipt uses one consistent database snapshot

- **WHEN** metadata/detail coverage changes while an audit is running
- **THEN** all subjects, bounds, and age decisions MUST reflect the one
  repeatable-read snapshot and captured audit time

#### Scenario: Clone provenance binds to an existing origin state

- **WHEN** a clone references no origin or its origin model/source/time/URI/
  checksum or fingerprint disagrees with the clone provenance
- **THEN** the audit MUST block publication
- **AND** a valid clone MUST retain its own subject ID while sharing only the
  exact origin artifact coverage

#### Scenario: Evidence reads resist path replacement

- **WHEN** an evidence component is a symlink, becomes a symlink/inode after
  discovery, or a missing leaf is reached through an unsafe parent
- **THEN** descriptor-bound no-follow access MUST block publication
- **AND** manifest parsing and checksum verification MUST refer to the same
  opened inode

#### Scenario: State archive binds the referenced member

- **WHEN** a provider, legacy, or clone state archive tarball verifies but its
  manifest lacks a unique member matching the physical state URI and DB
  checksum
- **THEN** the audit MUST block publication rather than mark the state
  subject complete

#### Scenario: Receipt publication loses its parent or durability proof

- **WHEN** the configured parent is replaced during atomic publication or
  directory fsync fails after replacement
- **THEN** the audit MUST fail closed with an indeterminate-publication
  diagnostic and MUST NOT report a fresh published receipt
