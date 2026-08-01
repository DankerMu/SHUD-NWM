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
