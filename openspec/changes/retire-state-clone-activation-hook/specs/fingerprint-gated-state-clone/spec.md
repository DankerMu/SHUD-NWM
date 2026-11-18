## MODIFIED Requirements

### Requirement: Fingerprint-gated state clone at cutover

At cutover the mechanism SHALL clone the latest qualified `(M0, source, t*)` snapshot row in the caller-supplied state repository into a `(M1, source, t*)` row. For the clone CLI that repository is the scheduler file state index; the core also accepts the DB `hydro.state_snapshot` repository. The clone SHALL run only when the `M0` package and the `M1` package have an equal `hydrologic_core_fingerprint`. A source snapshot is **qualified** only when it is usable (`usable_flag=true`), QC-passing, carries a valid `checksum`, is the `+12h` successor checkpoint (`lead_hours = 12`), and its `valid_time` equals the cutover cycle boundary `t*` that `M1`'s first strict cycle warm-starts from (docs §Gate G10 condition 4: snapshot valid time must satisfy runtime time consistency with the run start); a stale snapshot (`valid_time < t*`, e.g. after failed final `M0` cycles) is NOT qualified. The physical SHUD state file SHALL NOT be copied. The fingerprint equality gate SHALL reuse `workers/mapping_builder/rewrite.py::verify_hydrologic_core_fingerprint_equal` and SHALL NOT reimplement the fingerprint rule.

The clone row's full column disposition is pinned so no lineage-checked column is left to guesswork:

- **Preserved verbatim from the source row**: `state_uri`, `checksum`, `source_id`, `valid_time`, `cycle_id`, `lead_hours`, `usable_flag`, `original_shud_filename`, and `run_id` (the `M0` producing run — physical provenance; see the provenance requirement).
- **Overwritten to the target**: `model_id` = `M1`; `model_package_version` = the `M1` package version (`M1`'s `core.model_instance.model_package_uri`, the value the strict validators compare — `services/orchestrator/chain.py::_validate_strict_state_lineage` and `packages/common/state_manager.py::_state_index_lineage_mismatch` both reject on a version mismatch); `model_package_checksum` = the `M1` package checksum.
- **New on the clone row**: `state_id` (minted per the provenance requirement), `created_at` (insert default), `cloned_from_state_id`, `cloned_from_model_id`, `clone_gate_fingerprint`.

#### Scenario: Equal fingerprint clones the row without copying the state file

- **WHEN** a cutover clones `(M0, source, t*)` into `(M1, source, t*)` and the `M0` and `M1` `hydrologic_core_fingerprint` are equal
- **THEN** a new state row is created in the caller-supplied state repository, keyed `(M1, source, t*)`
- **THEN** the clone row's `state_uri` and `checksum` equal the source row's, and no physical state file is copied
- **THEN** the clone row's `model_package_version` and `model_package_checksum` equal the `M1` package version and the `M1` package checksum
- **THEN** `source_id`, `valid_time`, `cycle_id`, `lead_hours`, `usable_flag`, and `original_shud_filename` equal the source row's values.

#### Scenario: Unequal fingerprint refuses the clone fail-closed

- **WHEN** a cutover attempts to clone `(M0, source, t*)` into `(M1, source, t*)` and the `M0` and `M1` `hydrologic_core_fingerprint` are not equal
- **THEN** the clone is refused fail-closed and no `(M1, source, t*)` row is written
- **THEN** the refusal surfaces the stable error code `state_clone_cold_start_approval_required` plus an audit record, through the caller's audit recorder, naming `m0_model_id`, `m1_model_id`, `source_id`, `cutover_valid_time` and the refusal scope. The fingerprint-inequality cause is among them. In `baseline_cutover` the caller may proceed only by declaring the basin cold (§11.3)
- **THEN** no partial state row and no physical file mutation are produced.

#### Scenario: Clone row satisfies the existing strict warm-start validator for M1

- **WHEN** `M1`'s first cycle selects a warm-start successor state under strict mode
- **THEN** the cloned `(M1, source, t*)` row is accepted as the exact successor because its `valid_time == cycle_time`, its `model_package_version` and `model_package_checksum` match the `M1` target, its `source_id` matches the target source, its `cycle_id` matches the expected producing cycle, and its `lead_hours` is the preserved `+12h` successor value
- **THEN** acceptance holds on both selection planes — the DB-path validator (`services/orchestrator/chain_forecast_state.py::_validate_strict_forecast_state`) and the file-state-index evidence path (`packages/common/state_manager.py::strict_warm_start_evidence`)
- **THEN** the existing `strict-warm-start` validator is used unchanged and no warm-start requirement is modified by this capability.

#### Scenario: No qualified source snapshot fails closed

- **WHEN** a clone call finds no usable, QC-passing `(M0, source, t*)` source snapshot for its source
- **THEN** the clone is refused fail-closed with no `(M1, source, t*)` row written
- **THEN** the failure surfaces the stable error code `state_clone_cold_start_approval_required` and a recorded audit blocker, not a silent no-op.

#### Scenario: A stale latest snapshot is not qualified

- **WHEN** at cutover the newest usable `(M0, source)` snapshot has `valid_time < t*` (for example because `M0`'s final cycles failed)
- **THEN** the snapshot does not qualify (docs §Gate G10 condition 4) and the clone is refused fail-closed exactly as in the missing-source case, so in `baseline_cutover` the caller may proceed only by declaring the basin cold
- **THEN** no clone row with a stale `valid_time` is ever written for `M1`, so `M1`'s first strict cycle can never be pointed at a checkpoint the strict `valid_time == cycle_time` selection would reject.

## REMOVED Requirements

### Requirement: The clone executes per source across the activation source scope

**Reason**: This requirement described the retired activation-time hook (#3189): the activation context's source scope,
and one transaction across all sources that rolls back the activation.

**Migration**: The node-22 clone CLI `scripts/node22_clone_direct_grid_cutover_states.py` clones per basin/source unit, each fail-closed. Durable abort evidence
for partially completed invocations is governed by "File-index clone invocations preserve durable abort evidence and
primary errors".
