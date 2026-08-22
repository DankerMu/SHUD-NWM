# ADR 0005: Package identity and state transferability are two predicates

Date: 2026-08-21

## Status

Accepted (issue #1697, change `recalibration-state-carryover`, migration
`db/migrations/000053_state_snapshot_clone_gate_kind.sql`)

## Context

`packages/common/state_clone.py::fingerprint_gated_state_clone` gates every
state clone on the G10 ten-surface `hydrologic_core_fingerprint`
(`workers/mapping_builder/rewrite.py`). Its ten surfaces are `calibration`,
`geol`, `lake`, `land`, `mesh`, `river`, `soil`, `solver_config`,
`sp_att_non_forc`, `state_schema`.

That predicate answers **"is this the same model package?"**. It is used to
answer **"is this state transferable?"**. The two questions differ on exactly
two surfaces:

- `calibration` — `cfg.calib` + `CALIB/*`
- `solver_config` — `cfg.para`

Changing a calibration parameter changes **how the model steps forward from a
state**. It does not change what the state vector *is*, nor the mesh / river /
soil / geol / land / lake geometry the state lives on, nor the `.sp.att`
non-`FORC` fields, nor the initial-condition schema.

The operational consequence: a **calibration-parameter-only** package update —
mesh, river, gis and IC all unchanged — was forced into a cold start plus
explicit approval. Huai-MAIN and jialingjiang (CJ-JLJ) are exactly this case.
Measured on the two packages: of the 26 required files only `cfg.calib` and
`cfg.para` changed; Huai-MAIN additionally changed `tsd.forc`, which is not one
of the ten surfaces at all. Owner ruling: a basin whose only change is
calibration parameters SHALL continue its computation rather than restart.

Two facts about the production shape of this route decided its details:

1. `scripts/provision_direct_grid_scheduler_registry.py` — the producer of the
   direct-grid variants in production — records **no** `hydrologic_core_fingerprint`
   anywhere in the variant manifest or the registry row. Only
   `workers/mapping_builder/evidence.py` records one, and the dg variants this
   route targets are not built through it.
2. `scripts/node22_clone_direct_grid_cutover_states.py` already passed
   `m1_recorded_hydrologic_core_fingerprint=fingerprint.hash` — the value it had
   just computed. The clone's evidence cross-check was therefore **already
   vacuous** on the tool path.

## Decision

**Add a second, narrower, explicitly declared predicate for the transferability
question, and leave the package-identity gate untouched as the default.**

`STATE_COMPATIBILITY_SURFACES` is the ten G10 labels minus `calibration` and
`solver_config` — eight surfaces. `compute_hydrologic_core_fingerprint` and
`verify_hydrologic_core_fingerprint_equal` take a keyword-only
`surfaces` argument defaulting to the full ten, so the **hashing algorithm is
unchanged**: one `f"{label}\t{hash}\n"` line per covered surface in alphabetical
label order, SHA-256 of the joined buffer. There is no second hash format and no
second class. An eight-surface value is a different value from a ten-surface one
by construction — a different line set — and the two are never compared.

`fingerprint_gated_state_clone` takes
`transfer_mode: Literal["fix_forward", "recalibration"] = "fix_forward"`.
`recalibration` gates on the eight surfaces and refuses with the new
`refusal_scope = "state_compatibility_unequal"`; every other check runs
unconditionally in both modes.

Three details are load-bearing and are the reason this ADR exists rather than a
one-line changelog entry:

- **Per-side gate bytes.** The clone took ONE `state_schema_bytes` /
  `solver_config_bytes` and passed each to BOTH sides of the equality gate. That
  was correct for July's baseline→variant cutover, where the variant copies the
  baseline's `cfg.ic` verbatim. Under `M1 → M1'` it is a **false pass**: if
  `M1'` ships a new `cfg.ic`, feeding one side's bytes to both makes the
  `state_schema` surface compare equal and admits a clone that must refuse. The
  optional `m0_state_schema_bytes` / `m0_solver_config_bytes` overrides default
  to `None` (today's behavior) and are always supplied by the recalibration
  tool.
- **Union-of-both-roots category enumeration.**
  `provision_direct_grid_scheduler_registry.py::_category_files` derives paths
  from ONE root and falls its `lake` category back to the package's `*.cfg.para`
  when the basin has no lake file. Under a recalibration `cfg.para` is exactly
  the file that changes, so single-root enumeration would hash it into the
  `lake` surface and refuse the very case this change exists to permit — and
  would additionally false-pass a lake file *removed* in `M1'`, since an
  `M1'`-derived list would never name it. The recalibration tool has its own
  builder taking the union of both roots; the `lake` placeholder, used only when
  the union is empty on both sides, is the package's `*.sp.mesh` (invariant
  under recalibration, already covered by the `mesh` surface, and visible in
  `covered_paths` as `lake:<basin>.sp.mesh`).
- **The evidence cross-check is explicitly waived, not vacuously satisfied.**
  `m1_recorded_hydrologic_core_fingerprint` becomes `str | None`. In
  `fix_forward` an absent or empty value still refuses with
  `evidence_fingerprint_mismatch` — the contract does not weaken. In
  `recalibration`, `None` **skips** the cross-check and the skip is recorded in
  the clone receipt, rather than being satisfied by the caller handing the gate
  back the value it just computed.

The clone row records which gate admitted it: `clone_gate_kind`
(`'hydrologic_core'` / `'state_compatibility'`, NULL on every pre-existing and
non-clone row), alongside `clone_gate_fingerprint`, which records the accepted
value **of that same gate**.

Scheduler admission and lineage validation are **not** touched.
`services/orchestrator/chain.py::_validate_state_lineage` already accepts a
clone row carrying the `M1'` `model_id` + `model_package_version` +
`model_package_checksum`; the July baseline→variant cutover proved the
downstream chain.

## Rejected alternatives

**(a) Keep one `model_id` and swap the package underneath it.** This is the
smallest-looking change — no new gate, no new column, no clone row at all.
It does not work, for two independent reasons. `model_id` is minted from
package identity, so the "same" `model_id` over a new package is a lie the
registry itself contradicts. And the lineage gate compares
`model_package_checksum`: the warm-start validator would reject the carried
state against the swapped package's checksum, which is precisely the invariant
that makes warm starts trustworthy. Defeating it would mean weakening the
checksum comparison for everyone, to buy a case that a narrower gate handles
without touching admission at all.

**(b) Extend the cutover declaration's `transition_mode` enum.** A
recalibration is superficially a "cutover", so declaring it through the existing
declaration schema looks like reuse. Under inspection there is nothing to
declare: the old dg row walks the existing `retire` path unchanged, and the new
row is `added`, which needs no declaration. The enum extension would therefore
add a schema version, a validator branch, and a migration for a value carrying
no information the clone receipt does not already carry — and the receipt, not
the declaration, is what an auditor reads to see which gate admitted the row.
Pure churn on a schema that other consumers must keep parsing.

**(c) A forced-clone bypass flag.** An operator-supplied
`--force-clone` / `allow_fingerprint_mismatch` would cover this case and every
future one in a single line. It is rejected because it converts a fail-closed
gate into an advisory one: the refusal path's whole value is that no human
judgement call stands between "surfaces differ" and "no row written". A bypass
also destroys the audit story — `clone_gate_fingerprint` would name a gate that
did not admit the row, and no reader could distinguish a proven carry-over from
an asserted one. The narrower gate keeps the refusal fail-closed and keeps the
row self-describing; a bypass would have made both untrue for every clone in the
table, not just the recalibration ones.

## Consequences

- The recalibration route is opt-in and the default path is byte-identical.
  Every new parameter is keyword-only with a behavior-preserving default; the
  ten-surface coverage assertion in `tests/test_mapping_builder_rewrite.py`
  still passes untouched.
- The eight remaining surfaces still refuse any mesh / river / soil / geol /
  land / lake / `.sp.att` non-`FORC` / `cfg.ic` drift, and the union
  enumeration catches an added *or* removed hydrologic-core file. A new
  `cfg.ic` alongside a calibration update refuses — intentionally: a new IC
  means the modeling side declared a new starting point.
- The spin-up-distortion announcement obligation is **retained**. Warm
  carry-over across a recalibration is lower distortion than a cold start but
  not zero, and the clone receipt is the declared carry-over evidence that
  records it.
- `hydro.state_snapshot` gains one nullable column. Both independently
  maintained copies of the upsert SQL —
  `packages/common/state_manager.py::PsycopgStateSnapshotRepository` and
  `packages/common/state_clone_hook.py::_CursorBoundStateSnapshotRepository`,
  the in-cutover-transaction production write path — must gain it in lockstep,
  in the INSERT column list, the `VALUES` arity, the `ON CONFLICT DO UPDATE SET`
  list and the parameter tuple. Deduplicating the two copies stayed out of
  scope; a static lockstep test in `tests/test_state_clone_cutover_hook.py`
  guards the divergence instead.
- Two indexes, one non-atomic write. The recalibration tool writes the NFS
  canonical index and the node-22-local scratch mirror in one invocation, gate
  run once, the same `StateSnapshot` object into both — which is what keeps the
  copyback merge's `current == source_entry` equality branch reachable. If the
  mirror write fails after the canonical write succeeded, the tool exits
  non-zero and the receipt names canonical-written / mirror-not-written. The
  failure mode is a stalled cycle (fail-safe under
  `NHMS_REQUIRE_FORECAST_WARM_START=true`); a distributed transaction across two
  filesystems would be disproportionate.
- Execution host is node-22: the only host that can reach both indexes, and
  DB-free, so the recalibration mode takes no DB handle.

## Revisit

- If a recalibration ever needs to allow a `soil` / `geol` / `land` change too,
  do NOT widen `STATE_COMPATIBILITY_SURFACES` by reflex. Those surfaces are
  parameters the state vector is defined against; re-derive the transferability
  argument for the specific surface first, and expect it to need a third named
  gate rather than a wider second one.
- If a direct-grid variant producer starts recording a real
  `hydrologic_core_fingerprint` in its evidence package, the D5 waiver stops
  being necessary for that producer: pass the recorded value and the
  cross-check runs against the eight-surface recompute.
- If a DB-backed recalibration caller ever appears, the gate itself is already
  repository-agnostic — but re-read the dual-index reasoning above before
  assuming the file plane can be dropped.
- **`:107` "Scheduler admission and lineage validation are not touched" is now
  qualified** (`#1735`, change `lineage-scoped-cycle-completion`). Leaving the
  scheduler alone was the half of the ruling that wedged production: every
  completeness predicate is keyed strictly by `model_id`, `M1'` has zero
  pipeline history before its cutover `t*`, so on 2026-08-22 all 29 cycles in
  the 336h backfill lookback flipped `complete` → `gap` and the lane pinned
  itself on a cycle `M1'` could never close. The scheduler now DOES consume
  `cloned_from_model_id` / the clone row's `valid_time`: **completion scope**
  and **cohort membership** exclude a model for cycles strictly earlier than
  its own `t*`, resolved per `(model_id, source_id)` from the model's own
  earliest clone row with no ancestry walk. What remains untouched is what
  this ADR's sentence was actually protecting: **admission-side** validation —
  `_validate_state_lineage`, the strict warm-start checks, the D8.3 / D8.7
  generation quarantine, and the derivation of the content-addressed
  `model_id`. A model with no clone row is scored and admitted byte-for-byte
  as before.
