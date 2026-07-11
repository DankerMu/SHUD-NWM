# fingerprint-gated-state-clone Specification

## Purpose
TBD - created by archiving change mapping-variant-state-compatibility. Update Purpose after archive.
## Requirements
### Requirement: Fingerprint-gated state clone at cutover

At cutover the mechanism SHALL clone the latest qualified `(M0, source, t*)` snapshot row in `hydro.state_snapshot` into a `(M1, source, t*)` row, and SHALL do so only when the `M0` package and the `M1` package have an equal `hydrologic_core_fingerprint`. A source snapshot is **qualified** only when it is usable (`usable_flag=true`), QC-passing, carries a valid `checksum`, is the `+12h` successor checkpoint (`lead_hours = 12`), and its `valid_time` equals the cutover cycle boundary `t*` that `M1`'s first strict cycle warm-starts from (docs §Gate G10 condition 4: snapshot valid time must satisfy runtime time consistency with the run start); a stale snapshot (`valid_time < t*`, e.g. after failed final `M0` cycles) is NOT qualified. The physical SHUD state file SHALL NOT be copied. The fingerprint equality gate SHALL reuse `workers/mapping_builder/rewrite.py::verify_hydrologic_core_fingerprint_equal` and SHALL NOT reimplement the fingerprint rule.

The clone row's full column disposition is pinned so no lineage-checked column is left to guesswork:

- **Preserved verbatim from the source row**: `state_uri`, `checksum`, `source_id`, `valid_time`, `cycle_id`, `lead_hours`, `usable_flag`, `original_shud_filename`, and `run_id` (the `M0` producing run — physical provenance; see the provenance requirement).
- **Overwritten to the target**: `model_id` = `M1`; `model_package_version` = the `M1` package version (`M1`'s `core.model_instance.model_package_uri`, the value the strict validators compare — `services/orchestrator/chain.py::_validate_strict_state_lineage` and `packages/common/state_manager.py::_state_index_lineage_mismatch` both reject on a version mismatch); `model_package_checksum` = the `M1` package checksum.
- **New on the clone row**: `state_id` (minted per the provenance requirement), `created_at` (insert default), `cloned_from_state_id`, `cloned_from_model_id`, `clone_gate_fingerprint`.

#### Scenario: Equal fingerprint clones the row without copying the state file

- **WHEN** a cutover clones `(M0, source, t*)` into `(M1, source, t*)` and the `M0` and `M1` `hydrologic_core_fingerprint` are equal
- **THEN** a new `hydro.state_snapshot` row is created keyed `(M1, source, t*)`
- **THEN** the clone row's `state_uri` and `checksum` equal the source row's, and no physical state file is copied
- **THEN** the clone row's `model_package_version` and `model_package_checksum` equal the `M1` package version and the `M1` package checksum
- **THEN** `source_id`, `valid_time`, `cycle_id`, `lead_hours`, `usable_flag`, and `original_shud_filename` equal the source row's values.

#### Scenario: Unequal fingerprint refuses the clone fail-closed

- **WHEN** a cutover attempts to clone `(M0, source, t*)` into `(M1, source, t*)` and the `M0` and `M1` `hydrologic_core_fingerprint` are not equal
- **THEN** the clone is refused fail-closed and no `(M1, source, t*)` row is written
- **THEN** the refusal surfaces the stable error code `state_clone_cold_start_approval_required` plus an `ops.audit_log` record naming the blocked `(basin_version_id, source_id)` scope and the fingerprint-inequality cause, degrading the path to the explicit cold-start approval route defined by `atomic-cutover-transaction` (§11.3)
- **THEN** no partial state row and no physical file mutation are produced.

#### Scenario: Clone row satisfies the existing strict warm-start validator for M1

- **WHEN** `M1`'s first cycle selects a warm-start successor state under strict mode
- **THEN** the cloned `(M1, source, t*)` row is accepted as the exact successor because its `valid_time == cycle_time`, its `model_package_version` and `model_package_checksum` match the `M1` target, its `source_id` matches the target source, its `cycle_id` matches the expected producing cycle, and its `lead_hours` is the preserved `+12h` successor value
- **THEN** acceptance holds on both selection planes — the DB-path validator (`services/orchestrator/chain_forecast_state.py::_validate_strict_forecast_state`) and the file-state-index evidence path (`packages/common/state_manager.py::strict_warm_start_evidence`)
- **THEN** the existing `strict-warm-start` validator is used unchanged and no warm-start requirement is modified by this capability.

#### Scenario: No qualified source snapshot fails closed

- **WHEN** an engaged clone step (per the `atomic-cutover-transaction` applicability predicate) finds no usable, QC-passing `(M0, source, t*)` source snapshot for a source in scope and no approved cold-start input covers that source
- **THEN** the clone is refused fail-closed with no `(M1, source, t*)` row written and the whole activation transaction rolls back
- **THEN** the failure surfaces the stable error code `state_clone_cold_start_approval_required` and a recorded audit blocker, not a silent no-op.

#### Scenario: A stale latest snapshot is not qualified

- **WHEN** at cutover the newest usable `(M0, source)` snapshot has `valid_time < t*` (for example because `M0`'s final cycles failed)
- **THEN** the snapshot does not qualify (docs §Gate G10 condition 4) and the clone is refused fail-closed exactly as in the missing-source case, routing to the explicit cold-start approval route
- **THEN** no clone row with a stale `valid_time` is ever written for `M1`, so `M1`'s first strict cycle can never be pointed at a checkpoint the strict `valid_time == cycle_time` selection would reject.

### Requirement: The clone executes per source across the activation source scope

The clone step SHALL enumerate every source in the activation context's source scope — the target variant's normalized `resource_profile.direct_grid_forcing.applicable_source_ids`, as supplied by the Change 4 hook context — and SHALL execute the fingerprint-gated clone once per source, producing one `(M1, source, t*)` row per source (GFS-driven and IFS-driven states at the same model/valid_time are distinct rows under the `(model_id, COALESCE(source_id,''), valid_time)` key). When any source in scope lacks a qualified `(M0, source, t*)` snapshot, the whole transaction SHALL roll back fail-closed unless an approved cold-start input explicitly covers that source (per `atomic-cutover-transaction`).

#### Scenario: Dual-source cutover clones one row per source

- **WHEN** a cutover activates an `M1` variant whose `applicable_source_ids` is `[gfs, ifs]` and qualified `(M0, gfs, t*)` and `(M0, ifs, t*)` snapshots both exist with equal fingerprints
- **THEN** two clone rows are written — `(M1, gfs, t*)` and `(M1, ifs, t*)` — each preserving its own source row's lineage
- **THEN** both rows commit atomically with the activation.

#### Scenario: One source missing a qualified snapshot rolls back the whole cutover

- **WHEN** the same dual-source cutover finds a qualified `(M0, gfs, t*)` snapshot but no qualified `(M0, ifs, t*)` snapshot, and no approved cold-start input covers `ifs`
- **THEN** the whole transaction rolls back: no clone row is written for either source and `M1` is not activated
- **THEN** the refusal's audit record names the blocking source (`ifs`), so the operator can either repair `M0`'s `ifs` state or re-request with an explicit per-source cold-start approval.

### Requirement: Fingerprint gate inputs are pinned to package and evidence authorities

The clone caller SHALL resolve the fingerprint-gate inputs from pinned authorities: the `M0` and `M1` package roots from each model's `core.model_instance.model_package_uri` (the NFS object-store package path); the `category_files` enumeration and both `.sp.att` paths from the `M1` variant's mapping manifest / mapping evidence package (the same inputs that produced the build-time G4 fingerprint); and real platform-level `state_schema_bytes` and `solver_config_bytes` — empty or missing byte inputs SHALL be refused fail-closed. The recomputed `M1` fingerprint SHALL additionally be cross-checked against the `hydrologic_core_fingerprint` value recorded in the `M1` mapping evidence package; a mismatch refuses the clone fail-closed, so gate equality can never be established from degenerate inputs supplied symmetrically to both sides.

#### Scenario: Empty fingerprint byte inputs are refused

- **WHEN** a clone is requested with empty `state_schema_bytes` or `solver_config_bytes` for either package
- **THEN** the clone is refused fail-closed with no row written, even though two packages both supplied empty bytes would compare equal under the degenerate inputs
- **THEN** the refusal is recorded and distinguishes invalid gate inputs from genuine fingerprint inequality.

#### Scenario: Recomputed variant fingerprint must match the recorded evidence value

- **WHEN** the clone gate recomputes the `M1` fingerprint and the value differs from the `hydrologic_core_fingerprint` recorded in the `M1` mapping evidence package
- **THEN** the clone is refused fail-closed with no row written, because the core-invariance claim the clone relies on is no longer proven for the supplied inputs
- **THEN** on any successful clone the recorded `clone_gate_fingerprint` equals both the recomputed value and the evidence-recorded value.

### Requirement: cloned_from provenance is recorded and existing rows are untouched

The clone SHALL record `cloned_from` provenance on the `(M1, source, t*)` row: the source `model_id` (`M0`), the source snapshot identity (`state_id`), and the gating `hydrologic_core_fingerprint` value that permitted the clone. Provenance SHALL be persisted in nullable columns that default `NULL`, so pre-clone and legacy snapshot rows remain valid and unchanged. The clone row's own primary key `state_id` SHALL be minted with the existing deterministic convention `packages/common/state_manager.py::state_snapshot_id(model_id, valid_time, source_id=…, cycle_id=…, lead_hours=…)` using the `M1` `model_id` and the preserved source/valid-time/cycle/lead inputs, so the ID embeds the new model identity, stays convention-compliant, and cannot collide with the source row's `state_id`. The clone row's `run_id` SHALL reuse the source `M0` producing run's `run_id` (satisfying the `NOT NULL` FK to `hydro.hydro_run` and documenting physical provenance); consumers attributing a state to a model MUST attribute by `model_id` plus `cloned_from_*` and MUST NOT attribute by `run_id` alone.

#### Scenario: Clone row records the provenance fields and a convention-minted identity

- **WHEN** a fingerprint-gated clone succeeds
- **THEN** the clone row records `cloned_from_model_id` equal to the source `M0` `model_id`, `cloned_from_state_id` equal to the source snapshot's `state_id`, and `clone_gate_fingerprint` equal to the gating fingerprint value
- **THEN** the clone row's `state_id` equals `state_snapshot_id(M1_model_id, valid_time, source_id=<preserved>, cycle_id=<preserved>, lead_hours=<preserved>)` and differs from the source row's `state_id`
- **THEN** the clone row's `run_id` equals the source `M0` producing run's `run_id`.

#### Scenario: Model attribution never relies on run_id alone

- **WHEN** a consumer attributes a cloned state row to a model
- **THEN** the attribution reads `model_id` (= `M1`) plus `cloned_from_model_id` (= `M0`); the `run_id` pointing at an `M0` run documents physical production, not model ownership
- **THEN** a warm-start-lineage read over the clone row yields `M1` as the owning model and `M0` as the transfer source, never `M0` as the owner.

#### Scenario: Pre-clone and legacy rows carry NULL provenance and stay valid

- **WHEN** a snapshot row was written before or outside any clone
- **THEN** its `cloned_from_state_id`, `cloned_from_model_id`, and `clone_gate_fingerprint` are `NULL`
- **THEN** the row keeps its existing identity and remains selectable by the unchanged warm-start path.

