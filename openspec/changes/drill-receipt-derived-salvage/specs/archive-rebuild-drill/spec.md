# archive-rebuild-drill Specification (delta)

## ADDED Requirements

### Requirement: Salvage input derives from the completeness receipt

When invoked with a completeness receipt, the rebuild drill SHALL derive its
`db-export` salvage-manifest set from that receipt's subjects with
`coverage=="db-export"` and `verdict=="complete"`, mapping each subject to its
archive manifest path under `<archive_root>/db-export/` using the salvage
tool's lane and identity mapping for both the `forcing` and `runs` lanes,
with subject identities path-safety-validated before any path join.
Subjects of lanes with no db-export mapping (e.g. `states`) SHALL be refused
or skipped with recorded evidence, never silently dropped. When a drop
window is supplied, derivation SHALL filter to subjects whose windows overlap
it under the retention gate's closed-interval overlap convention —
boundary-touching and zero-length intersections stay in scope — and the
drill receipt SHALL record the drop window used (or its absence). Explicitly
supplied salvage manifests SHALL be unioned with the derived set
(deduplicated by resolved path), and the drill receipt SHALL record each
input's provenance (derived vs explicit). The derived set SHALL be subject
to fail-closed bounds on cardinality and aggregate decompressed bytes in
addition to the existing per-object size cap.

Derivation SHALL be activated only by an explicit `--completeness-receipt`
flag or by a drill-scoped environment variable that no sibling tool sets.
A derivation yielding zero manifests SHALL NOT be a refusal; the drill SHALL
proceed and record the empty derived set as receipt evidence. An invocation
supplying a completeness receipt without any product archive manifest SHALL
be refused at configuration time, before any salvage input is read.

#### Scenario: Derived tuples cover the gate demand

- **WHEN** the drill runs with a completeness receipt and a drop window
- **THEN** for every salvage-backed demand window the retention gate derives
  from the same receipt and drop window, the window's clip to the drop
  window MUST be covered by the union of the drill receipt's db-export
  coverage tuples

#### Scenario: Missing manifest fails closed

- **WHEN** a derived subject's archive manifest file is absent or unreadable
- **THEN** the drill MUST emit a FAIL receipt naming the missing paths and
  exit non-zero, never a PASS over the narrower set

#### Scenario: Stale manifest window diverges from the receipt subject

- **WHEN** a derived manifest's selector window differs from the receipt
  subject's window
- **THEN** the drill MUST emit a FAIL receipt recording the divergence,
  because its coverage tuples take their windows from the manifest and a
  silent divergence would attest the wrong window

#### Scenario: No receipt supplied

- **WHEN** the drill runs without a completeness receipt
- **THEN** its behavior MUST be unchanged from the explicit
  `--salvage-manifest` whitelist contract

#### Scenario: A sibling tool's environment does not activate derivation

- **WHEN** the drill runs with no `--completeness-receipt` flag and no
  drill-scoped receipt-path variable, but the db-export salvage tool's own
  receipt-path variable is exported in the environment
- **THEN** derivation MUST stay off and the drill MUST behave exactly as the
  explicit `--salvage-manifest` whitelist contract

#### Scenario: Empty derivation is evidence, not a refusal

- **WHEN** a supplied completeness receipt yields no `db-export` +
  `complete` subject overlapping the drop window
- **THEN** the drill MUST NOT refuse; it MUST run its archive-manifest leg
  and record the empty derived set (derived count zero plus the drop window
  used) in the receipt, matching the retention gate, which demands no
  db-export coverage for that receipt and drop window

#### Scenario: Receipt without a product archive manifest is refused early

- **WHEN** the drill is invoked with a completeness receipt but no product
  archive manifest
- **THEN** it MUST be refused at configuration time with a message naming
  the archive-manifest flag, before any salvage input is read, because a
  PASS receipt requires at least one restored product cycle
