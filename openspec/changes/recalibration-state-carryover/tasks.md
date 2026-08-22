## 1. State-compatibility subgate (`workers/mapping_builder/rewrite.py`)

- [x] 1.1 Add `STATE_COMPATIBILITY_SURFACES` (the 8 labels of D1) and export it
  from `workers/mapping_builder/__init__.py` alongside
  `HYDROLOGIC_CORE_FINGERPRINT_LABELS`.
- [x] 1.2 Thread a keyword-only `surfaces: tuple[str, ...] =
  HYDROLOGIC_CORE_FINGERPRINT_LABELS` through
  `compute_hydrologic_core_fingerprint` and
  `verify_hydrologic_core_fingerprint_equal`. Derive `required_categories`
  from the surface set; compute only the surfaces in the set; assert
  `set(per_surface_hash) == set(surfaces)`. Validate `surfaces` is a
  non-empty duplicate-free subset of the ten labels, else `SpAttRewriteError`.
- [x] 1.3 Keep the hash format identical (per-surface line, alphabetical label
  order, SHA-256 of the joined buffer). No new format, no new class.

## 2. Clone gate (`packages/common/state_clone.py`)

- [x] 2.1 Add `transfer_mode: Literal["fix_forward", "recalibration"] =
  "fix_forward"`. `fix_forward` keeps the ten-surface gate and every existing
  check in existing order.
- [x] 2.2 `recalibration` gates on `STATE_COMPATIBILITY_SURFACES`; on
  `HydrologicCoreFingerprintMismatchError` **or** `MissingPackageFileError`
  refuse with `refusal_scope="state_compatibility_unequal"`, recording the
  missing side and relative path in the audit record when applicable — extend
  `_refuse` with an optional `extra` mapping merged into the record rather than
  changing any existing refusal's record shape. Export the new scope as a
  module constant.
- [x] 2.3 Add optional per-side gate bytes `m0_state_schema_bytes` /
  `m0_solver_config_bytes` (default `None` → reuse the target bytes, i.e.
  today's behavior). Extend the `degenerate_gate_inputs` refusal to cover a
  supplied-but-empty override.
- [x] 2.4 Make `m1_recorded_hydrologic_core_fingerprint` `str | None`. In
  `fix_forward` an absent/empty value refuses `evidence_fingerprint_mismatch`;
  in `recalibration` `None` skips the cross-check and records the skip
  (D5 waiver).
- [x] 2.5 Set `clone_gate_kind` on the clone row: `"hydrologic_core"` for
  `fix_forward`, `"state_compatibility"` for `recalibration`.

## 3. Provenance column (`clone_gate_kind`)

- [x] 3.1 `db/migrations/000053_state_snapshot_clone_gate_kind.sql`: nullable
  `TEXT DEFAULT NULL`, `ADD COLUMN IF NOT EXISTS`, no backfill, no index
  change — same house style as `000046`.
- [x] 3.2 Add the field to `StateSnapshot` (optional, default `None`, last in
  the clone-provenance block).
- [x] 3.3 Wire it through every storage surface named in the Invariant Matrix:
  `_snapshot_from_row`, `_snapshot_to_dict`, `_state_index_entry_from_snapshot`,
  `_state_snapshot_from_index_entry`, **and both** copies of the
  `hydro.state_snapshot` upsert SQL — INSERT column list + `VALUES` placeholders
  + `ON CONFLICT DO UPDATE SET` + parameter tuple in each:
  - `packages/common/state_manager.py::PsycopgStateSnapshotRepository.upsert_state_snapshot`
  - `packages/common/state_clone_hook.py::_CursorBoundStateSnapshotRepository.upsert_state_snapshot`
    — the in-cutover-transaction production write path; migration `000046`'s
    three columns are already threaded through it, and missing this copy means
    every hook-driven clone row stores `NULL`.
  Missing it from a `DO UPDATE SET` silently drops it on re-upsert; missing it
  from the hook's INSERT/params never writes it at all. Do **not** deduplicate
  the two SQL copies in this change — update them in lockstep.

## 4. Clone tool recalibration mode (`scripts/node22_clone_direct_grid_cutover_states.py`)

- [x] 4.1 Add `--transfer-mode {baseline_cutover,recalibration}` (default
  `baseline_cutover` = today's behavior), `--pairs M1:M1prime,...`,
  `--mirror-state-index`. Make the existing mode's flags optional at the parser
  level and enforce them **per mode after parsing** (no subparsers — the
  existing invocation must keep working verbatim): `baseline_cutover` still
  refuses a missing `--warm-basins`/`--cold-basins`/`--baseline-registry`/
  `--variant-registry`; `recalibration` refuses a missing `--pairs` or
  `--mirror-state-index`.
- [x] 4.2 Resolve each pair by `model_id` from the registry payload — never
  through `_variant_map` (D8). Validate per pair: both rows exist, both package
  roots resolve, both classify direct-grid, equal normalized
  `direct_grid_source_id`, `M1 != M1'`.
- [x] 4.3 Add `_state_compatibility_category_files(m0_root, m1_root)`: six
  categories, union of relative paths from both roots, `sp.mesh` placeholder
  for `lake` only when the union is empty on both sides (D4). Do not modify
  `provision_direct_grid_scheduler_registry.py::_category_files`.
- [x] 4.4 Read gate bytes **per side**. Under `recalibration` the clone's
  `m0_*` parameters mean the transfer **source** (`M1`) and the `m1_*`
  parameters the transfer **target** (`M1′`) — the names are not changed, so
  state the dual reading in the docstring. Concretely:
  `m0_state_schema_bytes` = M1's `*.cfg.ic`, `state_schema_bytes` = M1′'s
  `*.cfg.ic`; `m0_solver_config_bytes` = M1's `*.cfg.para`,
  `solver_config_bytes` = M1′'s.
- [x] 4.5 Run the gate once against the canonical repository, then upsert the
  same returned `StateSnapshot` object into the mirror repository (D7). Both
  repositories `create_missing=False`. A mirror-write failure exits non-zero
  with the receipt naming canonical-written / mirror-not-written.
- [x] 4.6 Receipt (`--receipt`, `O_EXCL` as today) records: pairs, `t*`,
  `transfer_mode`, per-pair 8-surface fingerprint, `clone_gate_kind`, the
  source-side and target-side `model_package_version`/`checksum`, and **both**
  index paths with per-index outcome.
- [x] 4.7 `--apply`/dry-run semantics preserved: dry-run runs every validation
  and the gate, writes no row into either index.

## 5. Specs, ADR, runbook

- [x] 5.1 `openspec/changes/.../specs/no-rollback-state-semantics/spec.md`:
  ADDED requirement "Recalibration state continuity routes by
  state-compatibility fingerprint" with the admit scenario, the refusal
  scenario, and the retained spin-up-distortion obligation.
- [x] 5.2 `openspec/changes/.../specs/fingerprint-gated-state-clone/spec.md`:
  MODIFIED requirement adding `transfer_mode`, `clone_gate_kind`, the per-side
  gate bytes, and the D5 cross-check waiver.
- [x] 5.3 `docs/adr/0005-recalibration-state-carryover.md`: why the ten-surface
  fingerprint is the wrong transferability predicate; rejected alternatives
  (a) same `model_id` with a swapped package, (b) extending the cutover
  declaration `transition_mode` enum, (c) a forced-clone bypass.
- [x] 5.4 `docs/runbooks/current-production-ops.md`: the recalibration
  (warm carry-over) procedure, the node-22 execution-host rule (D9), and the
  dual-index requirement.

## 6. Evidence mapping (risk packs → scenario-level proof)

Every row below is a required test with named input and expected output.

- [x] 6.1 **Run manifest / QC provenance pack** — recalibration admit: fixture
  packages differing only in `cfg.calib` + `CALIB/*` + `cfg.para` →
  `refused=False`, `clone_gate_kind="state_compatibility"`,
  `clone_gate_fingerprint` equals the independently computed 8-surface hash,
  `model_package_version`/`checksum` = M1′'s, `cloned_from_model_id` = M1,
  `state_uri`/`checksum`/`run_id`/`lead_hours` preserved.
  (Invariant Matrix row 1.)
- [x] 6.2 **SHUD runtime / restart-compatibility pack** — per-side `cfg.ic`
  drift: M1′ ships a different `*.cfg.ic` → `refused=True`,
  `refusal_scope="state_compatibility_unequal"`, no row in either index.
  This test MUST fail against a single-shared-bytes implementation.
  (Row 2; the D3 false-pass.)
- [x] 6.3 **Geospatial / basin-geometry pack** — one parametrized test per
  remaining surface (`mesh`, `river`, `soil`, `geol`, `land`,
  `sp_att_non_forc`): drift on that surface → refused with
  `state_compatibility_unequal`. (Row 3.)
- [x] 6.4 Lake union: (a) `*.lake.*` present only on M1′ → refused;
  (b) present only on M1 (removed in M1′) → refused — this one fails under a
  single-root enumeration; (c) absent on both → admitted and `covered_paths`
  contains `lake:<basin>.sp.mesh`. (Rows 4, 5.)
- [x] 6.5 Fix-forward regression: `uv run pytest -q tests/test_state_clone.py
  tests/test_state_clone_hook.py tests/test_mapping_builder_rewrite.py` green
  with no test edits; all six existing refusal scopes still reachable; the
  ten-label coverage assertion at `tests/test_mapping_builder_rewrite.py:1342`
  untouched. Plus a new test: `fix_forward` with
  `m1_recorded_hydrologic_core_fingerprint=None` → refused
  `evidence_fingerprint_mismatch`. (Rows 6, 7, 11.)
- [x] 6.6 **Published-artifact / display-identity pack** —
  `clone_gate_kind` round-trips: index entry with the key parses; index entry
  without it parses to `None`; a re-upsert through
  `state_manager.py`'s PG `DO UPDATE` path preserves the value. (Row 8.)
- [x] 6.6b **PostGIS / TimescaleDB pack** — hook write path: extend
  `tests/test_state_clone_cutover_hook.py` so its `FakeCursor` asserts
  `clone_gate_kind='hydrologic_core'` is persisted on a hook-driven clone. That
  fake reads provenance columns positionally out of the params tuple, so a
  column added to the SQL without the matching param (or the reverse) fails.
  Add a static lockstep check that both SQL copies name `clone_gate_kind`
  exactly once in the INSERT list, the `DO UPDATE SET` list, and the params
  tuple, with matching `VALUES` arity. (Rows 12, 13.)
- [x] 6.7 Dual-index write: after a recalibration run against two fixture
  indexes, `json.load` of both entry payloads for the clone key are equal
  dicts; a subsequent copyback merge of one into the other takes the
  `current == source_entry` branch and raises no
  `state_snapshot_index_copyback_conflict`. (Row 9.)
- [x] 6.8 `--pairs` resolution: an M1′ whose `resource_profile.baseline_model_id`
  names the original baseline still resolves; mismatched
  `direct_grid_source_id`, a legacy-classifying source model, and `M1 == M1'`
  each refuse before any write. (Row 10.)
- [x] 6.9 CLI end-to-end against fixture packages + fixture state indexes:
  dry-run writes nothing; `--apply` writes both indexes and a receipt whose
  fields match 4.6. Assert on the receipt JSON, not on stdout prose.

## 7. Verification commands

- [x] 7.1 `uv run ruff check .` — zero findings (local).
- [x] 7.2 `uv run pytest -q` — green (node-27 oracle; local run is
  necessary but not sufficient).
- [x] 7.3 `openspec validate recalibration-state-carryover --strict
  --no-interactive` — passes (local).
- [ ] 7.4 Migration `000053` applied on the node-27 real DB; `\d+
  hydro.state_snapshot` shows `clone_gate_kind text` nullable; an
  upsert→re-upsert round trip preserves the value.

## 8. Non-goals (explicit, no test owed)

- Scheduler admission / `_validate_state_lineage` changes — zero code touched.
- Widening the allowed-to-differ set beyond `calibration` + `solver_config`.
- A DB-backed recalibration caller.
- The #1698 / #1699 rollouts.

## Risk packs

| Pack | Selected | Reason |
|---|---|---|
| Run manifest / QC provenance | selected | The clone row + receipt IS the declared carry-over evidence; `clone_gate_kind` is new provenance. |
| SHUD numerical runtime / restart compatibility | selected | The gate decides whether a SHUD restart state is legal under a new package. |
| Published NHMS artifacts / display identity | selected | `StateSnapshot` identity flows into lineage validation and warm-start selection. |
| Geospatial / CRS / basin geometry | selected | Six of the eight surfaces are geometry/parameter files; drift on any must refuse. |
| PostGIS / TimescaleDB domain behavior | selected | Migration `000053` touches `hydro.state_snapshot` and both PG upsert copies; column-only, no hypertable/index change. Evidenced by 6.6b and 7.4. |
| Slurm production lifecycle / mock-vs-real parity | not selected | Zero scheduler/sbatch code touched; admission path unchanged by design. |
| Hydro-met time series / forcing windows | not selected | `tsd.forc`/FORC are outside both surface sets; forcing production untouched. |
| External hydro-met providers / snapshot reproducibility | not selected | No provider or acquisition code touched. |
