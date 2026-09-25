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

### Requirement: cloned_from provenance SHALL have a scheduling-time consumer

A clone row's `cloned_from_model_id` and `clone_gate_kind` SHALL be readable
at scheduling time on both persistence planes, and SHALL be consumed by the
scheduler's cycle-completion scope and cohort-admission decisions (see
`cross-cycle-warm-start-chaining`). They SHALL NOT remain write-only
provenance.

On the file state-snapshot index plane the fields SHALL survive entry
normalisation into the loaded index snapshot, so a scheduling pass resolves
lineage from data it has already loaded and performs no additional read. On the
database plane the resolution source SHALL be an **earliest**-clone-row read
under the model's own `model_id`, ordered `(valid_time, created_at)` ascending
— the model's existence-start. The publisher's descending reader
(`get_latest_clone_row_for_model_source`) SHALL NOT be the resolution source:
its ordering serves mirroring the just-committed row, not answering an
existence question, and reusing it would let a backdated re-activation
retroactively exclude cycles the identity actually ran.

Lineage admission SHALL be keyed on `cloned_from_model_id` alone — present,
non-empty, and different from the row's own `model_id`. `clone_gate_fingerprint`
is provenance recording WHICH gate admitted the clone and at WHAT value; it
SHALL NOT be an admission condition for lineage on either persistence plane. A
clone row carrying `cloned_from_model_id` but no `clone_gate_fingerprint` SHALL
therefore confer lineage, identically on both planes: requiring the fingerprint
would reject such a row and move `t*` LATER, which silently removes the model
from cycles it genuinely has a gap in, whereas admitting it leaves at most a
loud stuck gap.

That admission predicate binds the ANSWER, and both planes discharge every
clause while SELECTING the row. On the file state-snapshot index plane one
filter discharges all three clauses on the stripped value. On the database
plane the row-selection statement discharges presence, non-emptiness and the
difference from the row's own `model_id` on `cloned_from_model_id` with
surrounding whitespace removed (#2392; see the requirement "Both persistence
planes SHALL normalise cloned_from_model_id identically before judging it").
A provenance-corrupt row — a blank `cloned_from_model_id`, or one naming the
row's own `model_id` with surrounding whitespace — is therefore skipped by
selection on both planes and cannot MASK a later legitimate clone row; both
planes resolve the same `t*` for the same model. Downstream refusal of such a
row stays as defence in depth.

Lineage resolution SHALL distinguish a resolution FAILURE from a resolved "no
lineage". A state-snapshot index that exists but cannot be read, parsed, or
validated, a persistence-plane read that raises, and — on a plane whose
provider returns a structured signal the resolver can shape-check — a provider
that violates the resolution contract are failures; an absent provider, an
index that has
never been published, absent provenance, and provenance that does not establish
a predecessor are resolved answers meaning "no lineage". A never-published index
SHALL NOT be treated as a failure: a deployment that has performed no clone is a
healthy deployment with no lineage, and failing it would emit an operator signal
on every resolution forever. A failure SHALL surface
an operator-visible signal naming the model, the source, and the reason, and
SHALL NOT be memoized as "no lineage": a later resolution attempt for the same
`(model_id, source_id)` SHALL re-attempt the read rather than replay the
failure. A resolved "no lineage" MAY be memoized.

A reader SHALL tolerate the absence of these fields on an older or non-clone
entry, treating absence as "no lineage" rather than as an error.

#### Scenario: A scheduling pass resolves lineage without an extra read

- **WHEN** a db-free scheduling pass has loaded the file state-snapshot index
  and needs the lineage of a model that carries a clone row
- **THEN** it resolves `cloned_from_model_id` and the clone row's `valid_time`
  from the already-loaded index entries
- **THEN** it issues no additional read against the index or the object store.

#### Scenario: Absent provenance means no lineage, not an error

- **WHEN** a state-index entry or snapshot row carries no
  `cloned_from_model_id`
- **THEN** lineage resolution yields "no lineage" for that model and source
- **THEN** the model is scored and admitted exactly as a model that never
  cloned.

#### Scenario: A missing clone_gate_fingerprint does not withhold lineage

- **WHEN** a clone row or index entry carries `cloned_from_model_id` naming a
  different model but carries no `clone_gate_fingerprint`
- **THEN** both persistence planes admit it as the model's existence-start and
  resolve the same `t*` from its `valid_time`
- **THEN** neither plane treats the absent fingerprint as a reason to withhold
  lineage.

#### Scenario: A never-published index resolves to no lineage without a failure signal

- **WHEN** a lineage resolution runs on a deployment whose state-snapshot index
  has never been published
- **THEN** resolution yields "no lineage" for every model and source
- **THEN** no failure signal is emitted and the answer may be memoized, so a
  healthy deployment that has performed no clone stays quiet.

#### Scenario: A resolution failure is not remembered as "no lineage"

- **WHEN** a lineage resolution for `(model_id, source_id)` fails — the
  persistence-plane read raises, or a published state-snapshot index cannot be
  read, parsed, or validated
- **THEN** an operator-visible signal names the model, the source, and the
  failure reason
- **THEN** the failure is not stored as that pair's resolved lineage, and the
  next resolution for the same pair re-attempts the read
- **THEN** once the underlying condition clears, the pair resolves to its true
  cutover without restarting the scheduler process.

### Requirement: File-index clone invocations preserve durable abort evidence and primary errors

The node-22 file-index clone CLI SHALL require a persistent receipt path for every `recalibration` invocation, including dry-run. `baseline_cutover` SHALL retain its existing optional receipt flag.

When an `--apply` invocation has already persisted one or more clone rows and then aborts, the CLI SHALL attempt to write a receipt that enumerates every completed persisted clone and identifies the failed pair or basin/source location and failure reason before propagating the original failure. A receipt-write failure during this abort handling SHALL be exposed to the operator but SHALL NOT replace the original clone, validation, or mirror-write failure. A receipt-write failure on an otherwise successful invocation SHALL still fail normally, and an existing receipt SHALL never be overwritten.

#### Scenario: Baseline abort after a persisted clone produces evidence and stays failed

- **WHEN** `baseline_cutover --apply` persists at least one warm clone row and a later basin/source fails validation or clone admission
- **THEN** the requested receipt records every completed persisted clone with its `state_id`, marks the invocation aborted, and identifies the failed basin/source and original reason
- **THEN** the original exception still propagates and the process does not report success.

#### Scenario: Receipt failure cannot mask a recalibration refusal

- **WHEN** a recalibration apply has persisted an earlier pair, a later pair is refused, and the requested `O_EXCL` receipt path already exists
- **THEN** the original refusal exception propagates rather than `FileExistsError`
- **THEN** the operator-visible exception also reports that receipt persistence failed and names the receipt error, while the existing file remains unchanged.

#### Scenario: Receipt failure cannot mask mirror divergence

- **WHEN** a recalibration apply writes the canonical row, the mirror write fails, and receipt persistence also fails
- **THEN** the original mirror-divergence `CutoverCloneError` propagates rather than the receipt error
- **THEN** the operator-visible exception reports both the mirror failure and receipt persistence failure.

#### Scenario: Clean receipt failure remains a failure

- **WHEN** either mode otherwise completes but its requested receipt cannot be created
- **THEN** the receipt-write exception propagates and is not swallowed
- **THEN** an existing receipt is not overwritten.

#### Scenario: Recalibration always declares a receipt destination

- **WHEN** either a dry-run or `--apply` recalibration invocation omits `--receipt`
- **THEN** per-mode flag enforcement rejects it with the same parser-error shape as the other required recalibration flags
- **THEN** a successful invocation with a unique receipt path persists JSON equal to its returned receipt payload.

#### Scenario: Baseline compatibility and no-write failures remain unchanged

- **WHEN** an existing baseline-cutover invocation omits `--receipt`, or either mode fails before any clone row is persisted
- **THEN** baseline flag parsing remains valid and no new empty abort receipt is required
- **THEN** successful baseline receipt fields and dry-run state-index behavior retain their prior meanings.

### Requirement: The clone refuses a self-clone before every other gate

`fingerprint_gated_state_clone` SHALL refuse, fail-closed, when the source
model identity and the target model identity are the same value. The refusal
SHALL be evaluated BEFORE every other gate — before the no-reverse-clone
classification of the target's forcing-mapping manifest, before the degenerate
gate-input checks, before the qualified-source lookup, and before any
fingerprint computation — so a self-clone is refused on identity alone
regardless of how valid the remaining inputs are.

The refusal SHALL use the existing refusal channel: a compact audit record
naming a distinguished `refusal_scope` for self-clone, and the fail-closed
result. No `hydro.state_snapshot` row SHALL be written and no physical state
file SHALL be touched.

The reason the check belongs inside the gate rather than in each caller: the
clone row's `state_id` is minted deterministically from the TARGET `model_id`
plus the preserved source/valid-time/cycle/lead inputs, so with equal source and
target identities the minted `state_id` is byte-identical to the source row's
own `state_id` and the upsert overwrites the real source row in place — turning
it into a row that names itself as its own predecessor and losing the original
provenance irrecoverably. A reader-side guard can reject such a row afterwards
but cannot restore the overwritten one.

#### Scenario: A self-clone is refused in fix-forward mode

- **WHEN** `fingerprint_gated_state_clone` is invoked with the same value for
  the source and target model identities in the ten-surface `fix_forward` mode
- **THEN** the call returns refused with the self-clone `refusal_scope`
- **THEN** a refusal audit record is emitted naming that scope
- **THEN** no snapshot upsert is performed.

#### Scenario: A self-clone is refused in recalibration mode

- **WHEN** the same invocation is made in the eight-surface `recalibration`
  mode, including the branch where the target's recorded
  `hydrologic_core_fingerprint` is absent and the evidence cross-check is
  skipped
- **THEN** the call returns refused with the self-clone `refusal_scope` and no
  snapshot upsert is performed.

#### Scenario: Self-clone refusal outranks every other refusal

- **WHEN** a self-clone invocation supplies a valid direct-grid forcing-mapping
  manifest and non-empty state-schema and solver-config gate inputs, so no
  other gate would refuse
- **THEN** the refusal scope is the self-clone scope, not the
  target-not-direct-grid scope and not the degenerate-gate-inputs scope.

### Requirement: Both persistence planes SHALL normalise cloned_from_model_id identically before judging it

"Present, non-empty, and different from the row's own `model_id`" SHALL be judged on `cloned_from_model_id` with surrounding whitespace removed, where whitespace is exactly the set of characters Python's `str.isspace()` accepts. The comparison with `model_id` SHALL use the raw `model_id`. On the DB plane this SHALL be enforced by the SQL predicate of the earliest-clone-row reader, so that `LIMIT 1` never selects a row the file plane would skip. The resolver's own normalisation SHALL stay as defence in depth. The publisher's latest-clone-row reader is not governed by this requirement.

#### Scenario: A whitespace-only parent does not mask a later clone row

- **WHEN** the earliest clone row of a `(model_id, source_id)` has a `cloned_from_model_id` made only of whitespace (including tab, newline or U+3000) and a later clone row names a real predecessor
- **THEN** both planes SHALL resolve the lineage cutover from the later row

#### Scenario: A padded self-reference does not mask a later clone row

- **WHEN** the earliest clone row's `cloned_from_model_id` is the row's own `model_id` with surrounding whitespace and a later clone row names a real predecessor
- **THEN** both planes SHALL resolve the lineage cutover from the later row

#### Scenario: The planes agree

- **WHEN** the same set of clone rows is written to the file plane and to the DB plane
- **THEN** the two planes SHALL resolve the same `LineageCutover`
