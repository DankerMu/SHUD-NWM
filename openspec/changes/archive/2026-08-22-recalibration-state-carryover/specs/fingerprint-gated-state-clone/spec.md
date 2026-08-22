## MODIFIED Requirements

### Requirement: Fingerprint gate inputs are pinned to package and evidence authorities

The clone caller SHALL resolve the fingerprint-gate inputs from pinned
authorities: the `M0` and `M1` package roots from each model's
`core.model_instance.model_package_uri` (the NFS object-store package path);
the `category_files` enumeration and both `.sp.att` paths from the mapping
manifest / mapping evidence package (the same inputs that produced the
build-time G4 fingerprint); and real platform-level `state_schema_bytes` and
`solver_config_bytes` — empty or missing byte inputs SHALL be refused
fail-closed.

The gate SHALL accept **per-side** `state_schema_bytes` and
`solver_config_bytes`. When only one set is supplied it applies to both sides,
which is the pinned behavior for a legacy-to-variant cutover where the variant
copies the baseline's bytes verbatim. When a route's two packages may legally
carry different bytes for those surfaces — the `recalibration` route — the
caller SHALL supply each side's own bytes; supplying one side's bytes to both
sides would establish equality on a surface that actually differs.

The recomputed `M1` fingerprint SHALL be cross-checked against the
`hydrologic_core_fingerprint` value recorded in the `M1` mapping evidence
package whenever such a recorded value exists; a mismatch refuses the clone
fail-closed, so gate equality can never be established from degenerate inputs
supplied symmetrically to both sides. In `transfer_mode='fix_forward'` a
recorded value is REQUIRED and an absent or empty value SHALL refuse the clone
with the `evidence_fingerprint_mismatch` scope. In
`transfer_mode='recalibration'` the recorded value MAY be absent — the
direct-grid variants this route operates on are produced by
`scripts/provision_direct_grid_scheduler_registry.py`, which records no
`hydrologic_core_fingerprint` — in which case the cross-check SHALL be skipped
and the skip SHALL be recorded in the clone receipt, rather than satisfied
vacuously by the caller supplying back the value the gate just computed.

#### Scenario: Empty fingerprint byte inputs are refused

- **WHEN** a clone is requested with empty `state_schema_bytes` or
  `solver_config_bytes` for either package, in either `transfer_mode`
- **THEN** the clone is refused fail-closed with no row written, even though
  two packages both supplying empty bytes would compare equal under the
  degenerate inputs
- **THEN** the refusal is recorded and distinguishes invalid gate inputs from
  genuine fingerprint inequality.

#### Scenario: Recomputed variant fingerprint must match the recorded evidence value

- **WHEN** the clone gate recomputes the `M1` fingerprint, a recorded evidence
  value exists, and the two differ
- **THEN** the clone is refused fail-closed with no row written, because the
  core-invariance claim the clone relies on is no longer proven for the
  supplied inputs
- **THEN** on any successful clone the recorded `clone_gate_fingerprint` equals
  both the recomputed value and the evidence-recorded value.

#### Scenario: Fix-forward without a recorded evidence value is refused

- **WHEN** a `transfer_mode='fix_forward'` clone is requested with an absent or
  empty `m1_recorded_hydrologic_core_fingerprint`
- **THEN** the clone is refused fail-closed with the
  `evidence_fingerprint_mismatch` scope and no row written
- **THEN** the fix-forward cross-check obligation is not weakened by the
  recalibration route's waiver.

#### Scenario: Recalibration without a recorded evidence value skips the cross-check explicitly

- **WHEN** a `transfer_mode='recalibration'` clone is requested with no
  recorded evidence value, because the target variant was produced by the
  direct-grid provisioning script which records none
- **THEN** the cross-check is skipped, the clone proceeds on the eight-surface
  equality gate alone, and the skip is recorded in the clone receipt
- **THEN** the mechanism does not accept a caller-supplied echo of its own
  freshly computed value as if it were independent evidence.

## ADDED Requirements

### Requirement: The clone row records which gate admitted it

Every clone row SHALL record `clone_gate_kind`, naming the gate that admitted
it: `'hydrologic_core'` when the ten-surface `hydrologic_core_fingerprint` gate
admitted the row, and `'state_compatibility'` when the eight-surface
state-compatibility gate admitted it. `clone_gate_fingerprint` SHALL record the
accepted fingerprint value of that same gate, so the pair
`(clone_gate_kind, clone_gate_fingerprint)` is self-describing and the two
values are never compared across kinds.

`clone_gate_kind` SHALL be nullable and default to `NULL`, so pre-existing rows
and every non-clone snapshot row keep their identity unchanged and are not
rewritten. It SHALL be carried on both persistence planes — the
`hydro.state_snapshot` table and the file state-snapshot index entry — and a
reader SHALL tolerate its absence on an older entry.

#### Scenario: Audit distinguishes the two admissions

- **WHEN** an auditor reads two clone rows, one admitted by the ten-surface
  gate and one by the eight-surface gate
- **THEN** the first carries `clone_gate_kind='hydrologic_core'` and the second
  `clone_gate_kind='state_compatibility'`
- **THEN** the auditor can determine, from the row alone and without
  re-deriving package contents, which surface set was proven equal.

#### Scenario: Pre-existing and non-clone rows are unaffected

- **WHEN** a snapshot row written before this capability, or any row produced
  by the ordinary forecast save-state path, is read on either persistence plane
- **THEN** `clone_gate_kind` is `NULL`/absent and the row is otherwise
  unchanged
- **THEN** the unchanged warm-start selection and lineage validators accept it
  exactly as before.

#### Scenario: A re-upsert preserves the recorded gate kind

- **WHEN** an existing clone row is upserted again on the same
  `(model_id, source_id, valid_time)` identity
- **THEN** `clone_gate_kind` is carried through the conflict-update path and is
  not silently reset to `NULL`.
