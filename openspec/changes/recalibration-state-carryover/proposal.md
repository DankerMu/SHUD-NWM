## Why

The platform treats **any** model-package change as a new model: `M1→M1′` state
carry-over is permitted only when the G10 ten-surface `hydrologic_core_fingerprint`
is byte-equal (`packages/common/state_clone.py`,
`openspec/specs/no-rollback-state-semantics/spec.md` "Fix-forward state
continuity routes by fingerprint"). Two of those ten surfaces are `calibration`
(`cfg.calib` + `CALIB/*`) and `solver_config` (`cfg.para`).

Consequently a **calibration-parameter-only** package update — mesh / river /
gis / IC all unchanged — is forced into a cold start plus explicit approval.
Huai-MAIN and jialingjiang (CJ-JLJ) are exactly this case (measured: of the 26
required files only `cfg.calib` / `cfg.para` changed; Huai-MAIN additionally
changed `tsd.forc`, which is not one of the ten surfaces). Owner ruling: a
basin whose only change is calibration parameters SHALL continue its
computation rather than restart.

The ten-surface fingerprint is a **package-identity** predicate, not a
**state-transferability** predicate. This change adds a narrower, explicitly
declared subgate for the transferability question and leaves the default
package-identity gate untouched.

## What Changes

- Add a `STATE_COMPATIBILITY_SURFACES` surface set (the ten G10 labels minus
  `calibration` and `solver_config` — 8 surfaces: `geol`, `lake`, `land`,
  `mesh`, `river`, `soil`, `sp_att_non_forc`, `state_schema`) and thread a
  surface-set parameter through `compute_hydrologic_core_fingerprint` /
  `verify_hydrologic_core_fingerprint_equal`, reusing the same
  domain-separated hash format — no new hash format.
- Add `transfer_mode: Literal["fix_forward", "recalibration"]` to
  `fingerprint_gated_state_clone`, defaulting to `fix_forward` so every
  existing caller and every existing behavior is byte-identical.
  `recalibration` gates on the 8-surface subgate and refuses with the new
  `refusal_scope` `state_compatibility_unequal`.
- Record which gate admitted a clone row: new `clone_gate_kind` provenance
  column (`"hydrologic_core"` for `fix_forward`, `"state_compatibility"` for
  `recalibration`) on `hydro.state_snapshot`, on `StateSnapshot`, and in the
  file state-snapshot index entry.
- Extend `scripts/node22_clone_direct_grid_cutover_states.py` with an explicit
  `--transfer-mode recalibration --pairs <M1>:<M1′>,... --cutover-time <t*>`
  mode whose source and target are both direct-grid variants, which reads
  **per-side** `cfg.ic` / `cfg.para` bytes, and which writes the clone row into
  **both** state indexes (NFS canonical + scratch worker mirror) in one
  invocation.
- Two spec deltas plus ADR `docs/adr/0005-recalibration-state-carryover.md`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `no-rollback-state-semantics`: adds the recalibration state-continuity route
  and its refusal contract alongside the unchanged fix-forward route.
- `fingerprint-gated-state-clone`: adds `transfer_mode`, the state-compatibility
  subgate inputs, and the `clone_gate_kind` provenance column.

## Impact

- Code: `workers/mapping_builder/rewrite.py`, `packages/common/state_clone.py`,
  `packages/common/state_manager.py` (`StateSnapshot` + PG upsert + file index
  entry), `packages/common/state_clone_hook.py` (its own second copy of the
  `hydro.state_snapshot` upsert SQL — the in-transaction production write path),
  `scripts/node22_clone_direct_grid_cutover_states.py`,
  `db/migrations/000053_state_snapshot_clone_gate_kind.sql`.
- Scheduling / admission: **zero changes**. `services/orchestrator/chain.py`
  `_validate_state_lineage` already accepts a clone row carrying the `M1′`
  `model_id` + `model_package_version` + `model_package_checksum`; the July
  baseline→variant cutover proved the downstream chain.
- Operations: the tool runs on **node-22** (the only host that can reach both
  the NFS canonical index and the node-22-local scratch mirror); node-22 is
  DB-free, so the recalibration mode is file-index-only and takes no DB handle.
- Obligation retained: warm carry-over across a recalibration is lower
  distortion than a cold start but not zero — the spin-up-distortion
  announcement obligation still applies.

## Non-Goals

- Any change to scheduler admission or lineage validation logic.
- Widening the allowed-to-differ set beyond `calibration` + `solver_config`
  (YAGNI: the two basins driving this change touch nothing else).
- The actual Huai-MAIN / jialingjiang rollout (#1698) and new-basin onboarding
  (#1699).
- A DB-backed recalibration clone path. The `recalibration` mode is exercised
  through the file state-index repository only; the gate itself is
  repository-agnostic, but no DB-side caller is added here.
